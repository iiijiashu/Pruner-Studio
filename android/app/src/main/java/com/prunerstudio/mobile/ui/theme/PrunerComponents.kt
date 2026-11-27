package com.prunerstudio.mobile.ui.theme

import android.content.Context
import android.graphics.BitmapFactory
import android.graphics.drawable.BitmapDrawable
import android.media.MediaMetadataRetriever
import android.net.Uri
import android.os.Build
import android.util.Size as AndroidSize
import androidx.compose.animation.core.*
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.*
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.geometry.*
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.RectangleShape
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.*
import androidx.core.graphics.drawable.toDrawable
import androidx.core.net.toUri
import androidx.media3.common.util.UnstableApi
import androidx.palette.graphics.Palette
import coil.ImageLoader
import coil.compose.AsyncImage
import coil.decode.DataSource
import coil.disk.DiskCache
import coil.fetch.DrawableResult
import coil.fetch.FetchResult
import coil.fetch.Fetcher
import coil.memory.MemoryCache
import coil.request.ImageRequest
import coil.request.Options
import com.google.zxing.*
import com.google.zxing.qrcode.QRCodeWriter
import com.prunerstudio.mobile.core.model.Track
import com.prunerstudio.mobile.core.model.sanitizeExploreRatio
import com.prunerstudio.mobile.core.player.PrunerAppContainer
import java.io.File
import java.io.IOException
import java.util.Locale
import kotlin.math.PI
import kotlin.math.cos
import kotlin.math.roundToInt
import kotlin.math.sin
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.*
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

val controlShape = RoundedCornerShape(6.dp)
val surfaceShape = RoundedCornerShape(10.dp)

enum class ActionStyle {
    Primary,
    Secondary,
    Ghost,
}

private val GENERIC_REASON_PATTERN = Regex("^(网易云)?每日推荐$")

fun sanitizeReasonTag(raw: String?): String? =
    raw?.trim()?.takeIf { it.isNotEmpty() && !GENERIC_REASON_PATTERN.matches(it) }

fun formatTrackDuration(seconds: Double): String {
    if (!seconds.isFinite() || seconds <= 0.0) return ""
    val total = seconds.toInt()
    return String.format(Locale.ROOT, "%d:%02d", total / 60, total % 60)
}

@Composable
fun Panel(
    modifier: Modifier = Modifier,
    containerColor: Color = colors.surface,
    contentPadding: PaddingValues = PaddingValues(16.dp),
    spacing: Dp = 10.dp,
    content: @Composable ColumnScope.() -> Unit,
) {
    Surface(
        modifier,
        shape = surfaceShape,
        color = containerColor,
        border = BorderStroke(1.dp, colors.borderSubtle),
    ) {
        Column(
            Modifier.padding(contentPadding),
            verticalArrangement = Arrangement.spacedBy(spacing),
            content = content,
        )
    }
}

@Composable
fun Action(
    label: String,
    enabled: Boolean = true,
    fill: Color? = null,
    style: ActionStyle = ActionStyle.Secondary,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    val container =
        when {
            fill != null -> fill
            style == ActionStyle.Primary -> colors.primaryActionBg
            style == ActionStyle.Secondary -> colors.surface
            else -> Color.Transparent
        }
    val content =
        when {
            fill != null -> colors.semanticInk
            style == ActionStyle.Primary -> colors.primaryActionFg
            style == ActionStyle.Secondary -> colors.text
            else -> colors.secondary
        }
    val border =
        when {
            fill != null -> null
            style == ActionStyle.Secondary -> BorderStroke(1.dp, colors.borderMedium)
            else -> null
        }
    OutlinedButton(
        onClick = onClick,
        modifier = modifier.heightIn(min = 38.dp),
        enabled = enabled,
        shape = controlShape,
        border = border,
        colors =
            ButtonDefaults.outlinedButtonColors(
                containerColor = container,
                contentColor = content,
                disabledContainerColor =
                    if (style == ActionStyle.Ghost) Color.Transparent
                    else container.copy(alpha = 0.45f),
                disabledContentColor = content.copy(alpha = 0.45f),
            ),
        contentPadding = PaddingValues(horizontal = 14.dp, vertical = 7.dp),
    ) {
        Text(
            label,
            style = MaterialTheme.typography.labelLarge,
            fontWeight =
                if (fill != null || style == ActionStyle.Primary) FontWeight.SemiBold
                else FontWeight.Medium,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

@Composable
fun Glyph(
    icon: ImageVector,
    label: String,
    tint: Color = colors.text,
    modifier: Modifier = Modifier,
    click: () -> Unit,
) {
    IconButton(onClick = click, modifier = modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp)) {
        Icon(icon, label, tint = tint)
    }
}

/**
 * Virtualization-friendly grouped card wrapper for individual items inside a LazyColumn `itemsIndexed`.
 * Renders seamless top/middle/bottom paper card perimeter borders with zero inter-row gaps or
 * double hairlines while preserving 100% true LazyColumn virtualization.
 */
@Composable
fun GroupedRowCard(
    index: Int,
    count: Int,
    modifier: Modifier = Modifier,
    containerColor: Color = colors.surface,
    bottomSpacing: Dp = 12.dp,
    content: @Composable ColumnScope.() -> Unit,
) {
    val isFirst = index == 0
    val isLast = index == count - 1
    val shape =
        when {
            isFirst && isLast -> surfaceShape
            isFirst -> RoundedCornerShape(topStart = 10.dp, topEnd = 10.dp)
            isLast -> RoundedCornerShape(bottomStart = 10.dp, bottomEnd = 10.dp)
            else -> RectangleShape
        }
    val borderColor = colors.borderSubtle
    val perimeterModifier =
        if (isFirst && isLast) {
            Modifier
        } else {
            Modifier.drawWithContent {
                drawContent()
                val strokePx = 1.dp.toPx()
                val half = strokePx / 2f
                val w = size.width
                val h = size.height
                val r = 10.dp.toPx().coerceAtMost(minOf(w, h) / 2f)
                when {
                    isFirst -> {
                        val path =
                            Path().apply {
                                moveTo(half, h)
                                lineTo(half, r)
                                arcTo(
                                    rect = Rect(half, half, half + 2 * r, half + 2 * r),
                                    startAngleDegrees = 180f,
                                    sweepAngleDegrees = 90f,
                                    forceMoveTo = false,
                                )
                                lineTo(w - half - r, half)
                                arcTo(
                                    rect = Rect(w - half - 2 * r, half, w - half, half + 2 * r),
                                    startAngleDegrees = 270f,
                                    sweepAngleDegrees = 90f,
                                    forceMoveTo = false,
                                )
                                lineTo(w - half, h)
                            }
                        drawPath(path, color = borderColor, style = Stroke(width = strokePx))
                    }
                    isLast -> {
                        val path =
                            Path().apply {
                                moveTo(half, 0f)
                                lineTo(half, h - r)
                                arcTo(
                                    rect = Rect(half, h - half - 2 * r, half + 2 * r, h - half),
                                    startAngleDegrees = 180f,
                                    sweepAngleDegrees = -90f,
                                    forceMoveTo = false,
                                )
                                lineTo(w - half - r, h - half)
                                arcTo(
                                    rect =
                                        Rect(
                                            w - half - 2 * r,
                                            h - half - 2 * r,
                                            w - half,
                                            h - half,
                                        ),
                                    startAngleDegrees = 90f,
                                    sweepAngleDegrees = -90f,
                                    forceMoveTo = false,
                                )
                                lineTo(w - half, 0f)
                            }
                        drawPath(path, color = borderColor, style = Stroke(width = strokePx))
                    }
                    else -> {
                        drawLine(
                            color = borderColor,
                            start = Offset(half, 0f),
                            end = Offset(half, h),
                            strokeWidth = strokePx,
                        )
                        drawLine(
                            color = borderColor,
                            start = Offset(w - half, 0f),
                            end = Offset(w - half, h),
                            strokeWidth = strokePx,
                        )
                    }
                }
            }
        }

    Surface(
        modifier =
            modifier
                .fillMaxWidth()
                .padding(bottom = if (isLast) bottomSpacing else 0.dp)
                .then(perimeterModifier),
        shape = shape,
        color = containerColor,
        border = if (isFirst && isLast) BorderStroke(1.dp, borderColor) else null,
    ) {
        Column(Modifier.fillMaxWidth()) {
            Column(
                modifier = Modifier.fillMaxWidth().padding(horizontal = 6.dp, vertical = 2.dp),
                content = content,
            )
            if (!isLast) {
                HorizontalDivider(
                    modifier = Modifier.padding(horizontal = 12.dp),
                    color = borderColor,
                )
            }
        }
    }
}

@Composable
fun TrackRowSkeleton(modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition(label = "skeleton")
    val alpha by
        transition.animateFloat(
            initialValue = 0.35f,
            targetValue = 0.72f,
            animationSpec =
                infiniteRepeatable(
                    animation = tween(durationMillis = 850, easing = FastOutSlowInEasing),
                    repeatMode = RepeatMode.Reverse,
                ),
            label = "skeleton_alpha",
        )
    val fill = colors.surfaceWell.copy(alpha = alpha)
    Row(
        modifier.fillMaxWidth().padding(horizontal = 8.dp, vertical = 10.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(44.dp).clip(controlShape).background(fill))
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Box(Modifier.fillMaxWidth(0.62f).height(14.dp).clip(controlShape).background(fill))
            Box(Modifier.fillMaxWidth(0.38f).height(11.dp).clip(controlShape).background(fill))
        }
        Box(Modifier.width(32.dp).height(11.dp).clip(controlShape).background(fill))
    }
}

@Composable
fun EmptyStatePanel(
    icon: ImageVector,
    title: String,
    subtitle: String,
    modifier: Modifier = Modifier,
    actionLabel: String? = null,
    onAction: (() -> Unit)? = null,
) {
    Panel(
        modifier = modifier.fillMaxWidth(),
        containerColor = colors.surfaceWell,
        contentPadding = PaddingValues(horizontal = 18.dp, vertical = 20.dp),
        spacing = 8.dp,
    ) {
        Row(
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.Top,
        ) {
            Box(
                Modifier.size(38.dp)
                    .clip(controlShape)
                    .background(colors.surface)
                    .border(1.dp, colors.borderSubtle, controlShape),
                contentAlignment = Alignment.Center,
            ) {
                Icon(icon, null, tint = colors.accentText, modifier = Modifier.size(20.dp))
            }
            Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                Text(
                    title,
                    style = MaterialTheme.typography.titleMedium,
                    color = colors.text,
                )
                Text(
                    subtitle,
                    style = MaterialTheme.typography.bodySmall,
                    color = colors.secondary,
                )
            }
        }
        if (actionLabel != null && onAction != null) {
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                Action(actionLabel, style = ActionStyle.Primary, onClick = onAction)
            }
        }
    }
}

@Composable
fun TrackOverflowMenu(
    track: Track,
    onPlayNow: () -> Unit,
    onPlayNext: () -> Unit,
    onAddToEnd: () -> Unit,
    onToggleLike: (() -> Unit)? = null,
    isLiked: Boolean = track.liked,
    onCacheOffline: (() -> Unit)? = null,
    onNotInterested: (() -> Unit)? = null,
) {
    var open by remember { mutableStateOf(false) }
    Box {
        Glyph(
            icon = Icons.Outlined.MoreVert,
            label = "更多操作",
            tint = colors.secondary,
        ) {
            open = true
        }
        DropdownMenu(
            expanded = open,
            onDismissRequest = { open = false },
            containerColor = colors.surface,
        ) {
            DropdownMenuItem(
                text = { Text("立即播放", color = colors.text) },
                leadingIcon = { Icon(Icons.Outlined.PlayArrow, null, tint = colors.text) },
                onClick = {
                    open = false
                    onPlayNow()
                },
            )
            DropdownMenuItem(
                text = { Text("下一首播放", color = colors.text) },
                leadingIcon = { Icon(Icons.Outlined.QueuePlayNext, null, tint = colors.text) },
                onClick = {
                    open = false
                    onPlayNext()
                },
            )
            DropdownMenuItem(
                text = { Text("加入队列末尾", color = colors.text) },
                leadingIcon = { Icon(Icons.Outlined.AddToQueue, null, tint = colors.text) },
                onClick = {
                    open = false
                    onAddToEnd()
                },
            )
            if (onToggleLike != null) {
                DropdownMenuItem(
                    text = { Text(if (isLiked) "取消喜欢" else "喜欢此曲", color = colors.text) },
                    leadingIcon = {
                        Icon(
                            if (isLiked) Icons.Outlined.Favorite else Icons.Outlined.FavoriteBorder,
                            null,
                            tint = if (isLiked) colors.prune else colors.text,
                        )
                    },
                    onClick = {
                        open = false
                        onToggleLike()
                    },
                )
            }
            if (onCacheOffline != null && track.effectiveProvider() != "device") {
                DropdownMenuItem(
                    text = { Text("离线缓存", color = colors.text) },
                    leadingIcon = { Icon(Icons.Outlined.DownloadForOffline, null, tint = colors.text) },
                    onClick = {
                        open = false
                        onCacheOffline()
                    },
                )
            }
            if (onNotInterested != null) {
                HorizontalDivider(color = colors.borderSubtle)
                DropdownMenuItem(
                    text = { Text("不再推荐", color = colors.prune) },
                    leadingIcon = { Icon(Icons.Outlined.ThumbDownOffAlt, null, tint = colors.prune) },
                    onClick = {
                        open = false
                        onNotInterested()
                    },
                )
            }
        }
    }
}

@Composable
fun Segments(labels: List<String>, selected: Int, select: (Int) -> Unit) {
    Row(
        Modifier.fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .background(colors.surfaceWell)
            .border(1.dp, colors.borderSubtle, RoundedCornerShape(8.dp))
            .padding(3.dp),
        horizontalArrangement = Arrangement.spacedBy(3.dp),
    ) {
        labels.forEachIndexed { i, label ->
            val isSelected = i == selected
            val chipShape = RoundedCornerShape(6.dp)
            Box(
                Modifier.weight(1f)
                    .clip(chipShape)
                    .background(if (isSelected) colors.surface else Color.Transparent)
                    .then(
                        if (isSelected) Modifier.border(1.dp, colors.borderSubtle, chipShape)
                        else Modifier
                    )
                    .clickable { select(i) }
                    .padding(vertical = 9.dp),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    label,
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = if (isSelected) FontWeight.SemiBold else FontWeight.Medium,
                    color = if (isSelected) colors.text else colors.secondary,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}

@Composable
fun SectionHeader(title: String, trailing: @Composable RowScope.() -> Unit = {}) {
    Row(
        Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Heading(title)
        Row(
            horizontalArrangement = Arrangement.spacedBy(6.dp),
            verticalAlignment = Alignment.CenterVertically,
            content = trailing,
        )
    }
}

@Composable
fun NumberText(value: String, size: Int = 15, color: Color = colors.text) {
    Text(value, style = PrunerTypography.number, fontSize = size.sp, color = color)
}

@Composable
fun Meta(label: String) {
    Text(
        label,
        Modifier.clip(controlShape)
            .background(colors.surfaceWell)
            .border(1.dp, colors.borderSubtle, controlShape)
            .padding(horizontal = 8.dp, vertical = 4.dp),
        style = MaterialTheme.typography.labelMedium,
        color = colors.secondary,
    )
}

@Composable
fun Heading(label: String) {
    Text(label, style = MaterialTheme.typography.titleLarge, color = colors.text)
}

@Composable
fun Page(title: String, content: LazyListScope.() -> Unit) {
    LazyColumn(
        Modifier.fillMaxSize(),
        contentPadding = PaddingValues(16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Text(
                title,
                style = MaterialTheme.typography.headlineLarge,
                modifier = Modifier.padding(bottom = 8.dp),
            )
        }
        content()
    }
}

@Composable
fun VinylDiscPlaceholder(modifier: Modifier = Modifier) {
    val c = colors
    val discOuter =
        if (c.theme.isPaper) blend(c.surfaceWell, c.text, 0.14f)
        else blend(c.surfaceElevated, c.text, 0.08f)
    val discBody =
        if (c.theme.isPaper) blend(c.surfaceWell, c.text, 0.08f)
        else blend(c.surface, c.text, 0.05f)
    val groove =
        (if (c.theme.isPaper) c.text else Color.White).copy(alpha = if (c.theme.isPaper) 0.11f else 0.10f)
    val labelFill = blend(c.surface, c.accent, if (c.theme.isPaper) 0.24f else 0.28f)
    val spindleFill = c.surface
    Canvas(modifier.fillMaxSize()) {
        val radius = size.minDimension * 0.42f
        val center = Offset(size.width / 2f, size.height / 2f)
        drawCircle(color = discBody, radius = radius, center = center)
        drawCircle(
            color = discOuter,
            radius = radius,
            center = center,
            style = Stroke(width = (size.minDimension * 0.025f).coerceAtLeast(1f)),
        )
        for (factor in listOf(0.84f, 0.70f, 0.56f)) {
            drawCircle(
                color = groove,
                radius = radius * factor,
                center = center,
                style = Stroke(width = (size.minDimension * 0.012f).coerceAtLeast(0.8f)),
            )
        }
        drawCircle(color = labelFill, radius = radius * 0.34f, center = center)
        drawCircle(
            color = groove,
            radius = radius * 0.34f,
            center = center,
            style = Stroke(width = 1f),
        )
        drawCircle(color = spindleFill, radius = radius * 0.10f, center = center)
    }
}

@Composable
fun LivingAcousticOrb(
    ratio: Float,
    modifier: Modifier = Modifier,
    playing: Boolean = false,
) {
    val r = sanitizeExploreRatio(ratio)
    val c = colors
    val amberCore = rgb(0xd99e4e)
    val amberEdge = rgb(0xe7be7c)
    val sageCore = rgb(0x62a368)
    val sageEdge = rgb(0x8ec495)
    val indigoCore = rgb(0x4c8bf5)
    val indigoEdge = rgb(0x7c86ee)
    val primaryHue =
        if (r <= 0.5f) blend(amberCore, sageCore, r * 2f)
        else blend(sageCore, indigoCore, (r - 0.5f) * 2f)
    val secondaryHue =
        if (r <= 0.5f) blend(amberEdge, sageEdge, r * 2f)
        else blend(sageEdge, indigoEdge, (r - 0.5f) * 2f)
    val highlight = if (c.theme.isPaper) rgb(0xfefcf8) else blend(secondaryHue, Color.White, 0.55f)
    val transition = rememberInfiniteTransition(label = "acoustic_orb")
    val phase by
        transition.animateFloat(
            initialValue = 0f,
            targetValue = (2.0 * PI).toFloat(),
            animationSpec =
                infiniteRepeatable(
                    animation = tween(durationMillis = if (playing) 3400 else 5600, easing = LinearEasing),
                    repeatMode = RepeatMode.Restart,
                ),
            label = "orb_phase",
        )
    Canvas(modifier) {
        val cx = size.width / 2f
        val cy = size.height / 2f
        val baseRadius = size.minDimension * 0.36f
        val cadenceBoost = 1f + r * 0.45f + (if (playing) 0.25f else 0f)
        val breath = sin(phase) * (0.04f + r * 0.025f) * (if (playing) 1.35f else 1f)
        val coreRadius = baseRadius * (1f + breath)
        val driftX = cos(phase * 0.7f) * size.minDimension * (0.04f + r * 0.03f)
        val driftY = sin(phase * 0.9f) * size.minDimension * (0.04f + r * 0.03f)

        // Outer soft radial aura
        drawCircle(
            brush =
                Brush.radialGradient(
                    colors =
                        listOf(
                            secondaryHue.copy(alpha = if (c.theme.isPaper) 0.32f else 0.36f),
                            primaryHue.copy(alpha = 0.12f),
                            Color.Transparent,
                        ),
                    center = Offset(cx, cy),
                    radius = size.minDimension * 0.50f,
                ),
            radius = size.minDimension * 0.50f,
            center = Offset(cx, cy),
        )

        // Concentric harmonic acoustic rings
        for (ring in 0..2) {
            val ringPhase = (phase + ring * 2.094f)
            val expand = 1.06f + ring * 0.12f + sin(ringPhase) * 0.035f * cadenceBoost
            val ringAlpha = (0.28f - ring * 0.07f).coerceAtLeast(0.08f)
            drawCircle(
                color = (if (ring % 2 == 0) primaryHue else secondaryHue).copy(alpha = ringAlpha),
                radius = (baseRadius * expand).coerceAtMost(size.minDimension * 0.48f),
                center = Offset(cx + driftX * 0.25f, cy + driftY * 0.25f),
                style = Stroke(width = (1.6f - ring * 0.3f).dp.toPx()),
            )
        }

        // Main fluid core gradient
        drawCircle(
            brush =
                Brush.radialGradient(
                    colors =
                        listOf(
                            highlight.copy(alpha = 0.92f),
                            secondaryHue.copy(alpha = 0.92f),
                            primaryHue.copy(alpha = 0.96f),
                        ),
                    center = Offset(cx - coreRadius * 0.24f + driftX, cy - coreRadius * 0.24f + driftY),
                    radius = coreRadius * 1.35f,
                ),
            radius = coreRadius,
            center = Offset(cx, cy),
        )

        // Crisp specular rim
        drawCircle(
            color = highlight.copy(alpha = if (c.theme.isPaper) 0.55f else 0.32f),
            radius = coreRadius,
            center = Offset(cx, cy),
            style = Stroke(width = 1.dp.toPx()),
        )
    }
}

@androidx.annotation.OptIn(UnstableApi::class)
internal val LocalPrunerAppContainer = staticCompositionLocalOf<PrunerAppContainer?> { null }

@androidx.annotation.OptIn(UnstableApi::class)
internal fun resolveCoverRequestUrl(
    url: String,
    sizeTier: Int,
    ctx: Context,
    appOverride: PrunerAppContainer? = null,
): Any? {
    val raw = url.trim()
    if (raw.isEmpty()) return null
    if (raw.startsWith("content://", ignoreCase = true) || raw.startsWith("file://", ignoreCase = true)) {
        return raw.toUri()
    }
    val app = appOverride ?: runCatching { PrunerAppContainer.get(ctx) }.getOrNull()
    val candidate =
        if (raw.startsWith("/api/lan/v1/")) {
            app?.sources?.resolveLanCoverUrl(raw) ?: return null
        } else if (
            raw.startsWith("http://", ignoreCase = true) &&
                (".126.net" in raw || ".music.163.com" in raw)
        ) {
            raw.replaceFirst(Regex("^http://", RegexOption.IGNORE_CASE), "https://")
        } else raw
    val parsed = candidate.toHttpUrlOrNull() ?: return null
    val allowed =
        if (app != null) app.sources.isAllowedCoverUrl(parsed)
        else parsed.isHttps && (parsed.host.endsWith(".126.net") || parsed.host.endsWith(".music.163.com")) && parsed.port == 443
    if (!allowed) return null
    // Discrete cover sizing per DESIGN.md (96 / 192 / 384) for NetEase 126.net images
    return if (parsed.host.endsWith(".126.net") && parsed.queryParameter("param") == null) {
        val tier = when {
            sizeTier <= 96 -> 96
            sizeTier <= 192 -> 192
            else -> 384
        }
        parsed.newBuilder().addQueryParameter("param", "${tier}y${tier}").build().toString()
    } else {
        parsed.toString()
    }
}

@androidx.annotation.OptIn(UnstableApi::class)
@Composable
fun Cover(
    url: String,
    modifier: Modifier = Modifier,
    sizeTier: Int = 192,
    onPaletteExtracted: ((Color?) -> Unit)? = null,
) {
    val ctx = LocalContext.current.applicationContext
    val density = LocalDensity.current
    val appOverride = LocalPrunerAppContainer.current
    val app =
        remember(ctx, appOverride) {
            appOverride ?: runCatching { PrunerAppContainer.get(ctx) }.getOrNull()
        }
    val pairingState = app?.pairingState?.collectAsState()
    val sourcesRev = app?.sources?.sourcesRevision?.collectAsState()
    val loader = remember(ctx) { coverLoader(ctx) }
    val resolvedData =
        remember(url, sizeTier, app, pairingState?.value, sourcesRev?.value) {
            resolveCoverRequestUrl(url, sizeTier, ctx, appOverride = app)
        }
    val targetPx = remember(sizeTier, density) { (sizeTier * density.density).roundToInt().coerceIn(96, 768) }
    val request =
        remember(resolvedData, targetPx, onPaletteExtracted != null) {
            resolvedData?.let { data ->
                ImageRequest.Builder(ctx)
                    .data(data)
                    .size(targetPx)
                    .crossfade(220)
                    .allowHardware(onPaletteExtracted == null)
                    .build()
            }
        }
    LaunchedEffect(resolvedData, onPaletteExtracted != null) {
        if (onPaletteExtracted == null) return@LaunchedEffect
        if (request == null) {
            onPaletteExtracted(null)
            return@LaunchedEffect
        }
        val color =
            withContext(Dispatchers.IO) {
                runCatching {
                    val result = loader.execute(request)
                    val bitmap = (result.drawable as? BitmapDrawable)?.bitmap ?: return@runCatching null
                    val palette = Palette.from(bitmap).maximumColorCount(12).generate()
                    val swatch =
                        palette.mutedSwatch
                            ?: palette.vibrantSwatch
                            ?: palette.darkMutedSwatch
                            ?: palette.dominantSwatch
                    swatch?.rgb?.let { Color(it) }
                }
                    .getOrNull()
            }
        onPaletteExtracted(color)
    }
    Box(
        modifier
            .clip(controlShape)
            .background(colors.surfaceWell)
            .border(1.dp, colors.borderSubtle, controlShape),
        contentAlignment = Alignment.Center,
    ) {
        VinylDiscPlaceholder()
        if (request != null) {
            AsyncImage(
                model = request,
                contentDescription = null,
                imageLoader = loader,
                modifier = Modifier.fillMaxSize(),
                contentScale = ContentScale.Crop,
            )
        }
    }
}

private class LocalAudioArtFetcher(
    private val context: Context,
    private val uri: Uri,
    private val options: Options,
) : Fetcher {
    private fun decodeDownsampled(bytes: ByteArray, maxDim: Int = 384): android.graphics.Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        val w = bounds.outWidth
        val h = bounds.outHeight
        if (w <= 0 || h <= 0) return null
        var sample = 1
        while ((w / sample) > maxDim * 2 || (h / sample) > maxDim * 2) {
            sample *= 2
        }
        return BitmapFactory.decodeByteArray(
            bytes,
            0,
            bytes.size,
            BitmapFactory.Options().apply { inSampleSize = sample },
        )
    }

    private fun isLikelyImageUri(): Boolean {
        val str = uri.toString().lowercase(Locale.ROOT)
        if ("/albumart/" in str) return true
        val mime = runCatching { context.contentResolver.getType(uri) }.getOrNull().orEmpty()
        return mime.startsWith("image/", ignoreCase = true)
    }

    override suspend fun fetch(): FetchResult? =
        withContext(Dispatchers.IO) {
            val bitmap =
                runCatching {
                    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                        context.contentResolver.loadThumbnail(uri, AndroidSize(384, 384), null)
                    } else null
                }
                    .getOrNull()
                    ?: if (isLikelyImageUri()) {
                        runCatching {
                            context.contentResolver.openInputStream(uri)?.use { stream ->
                                val raw = stream.readBytes()
                                decodeDownsampled(raw)
                            }
                        }.getOrNull()
                    } else {
                        null
                    }
                    ?: runCatching {
                        MediaMetadataRetriever().run {
                            try {
                                setDataSource(context, uri)
                                embeddedPicture?.let { bytes -> decodeDownsampled(bytes) }
                            } finally {
                                release()
                            }
                        }
                    }
                        .getOrNull()
            bitmap?.let {
                DrawableResult(
                    drawable = it.toDrawable(context.resources),
                    isSampled = true,
                    dataSource = DataSource.DISK,
                )
            }
        }

    class Factory(private val context: Context) : Fetcher.Factory<Uri> {
        override fun create(data: Uri, options: Options, imageLoader: ImageLoader): Fetcher? {
            if (data.scheme != "content" && data.scheme != "file") return null
            return LocalAudioArtFetcher(context, data, options)
        }
    }
}

@Volatile private var sharedLoader: ImageLoader? = null

@androidx.annotation.OptIn(UnstableApi::class)
@Synchronized
private fun coverLoader(ctx: Context): ImageLoader =
    sharedLoader
        ?: run {
            val defaultHttp =
                OkHttpClient.Builder()
                    .followRedirects(false)
                    .followSslRedirects(false)
                    .build()
            val routingCallFactory = Call.Factory { request ->
                val url = request.url
                val app = runCatching { PrunerAppContainer.get(ctx) }.getOrNull()
                val lan = app?.activeLan()
                if (
                    lan != null &&
                        !lan.isClosed &&
                        url.isHttps &&
                        url.host == lan.base.host &&
                        url.port == lan.base.port &&
                        url.encodedPath == "/api/lan/v1/cover"
                ) {
                    lan.readClient.newCall(request)
                } else if (
                    (url.isHttps && (url.host.endsWith(".126.net") || url.host.endsWith(".music.163.com")) && url.port == 443) ||
                        app?.sources?.isAllowedCoverUrl(url) == true
                ) {
                    defaultHttp.newCall(request)
                } else {
                    throw IOException("Invalid cover origin: ${url.host}")
                }
            }
            ImageLoader.Builder(ctx)
                .callFactory(routingCallFactory)
                .components { add(LocalAudioArtFetcher.Factory(ctx)) }
                .memoryCache {
                    MemoryCache.Builder(ctx).maxSizePercent(0.20).build()
                }
                .diskCache {
                    DiskCache.Builder()
                        .directory(File(ctx.cacheDir, "pruner_cover_cache"))
                        .maxSizeBytes(64L * 1024L * 1024L)
                        .build()
                }
                .respectCacheHeaders(false)
                .build()
                .also { sharedLoader = it }
        }

@Composable
fun TrackRow(
    track: Track,
    click: () -> Unit,
    active: Boolean = false,
    durationText: String? = null,
    reasonTag: String? = null,
    trailing: @Composable () -> Unit = {},
) {
    val cleanReason = sanitizeReasonTag(reasonTag)
    val duration = durationText ?: formatTrackDuration(track.durationSeconds())
    val rowBg = if (active) blend(colors.surface, colors.accent, 0.12f) else Color.Transparent
    Row(
        Modifier.fillMaxWidth()
            .clip(controlShape)
            .background(rowBg)
            .clickable(onClick = click)
            .padding(horizontal = 8.dp, vertical = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (active) {
            Box(
                Modifier.width(3.dp)
                    .height(34.dp)
                    .clip(RoundedCornerShape(2.dp))
                    .background(colors.accent)
            )
        }
        Cover(track.cover_url, Modifier.size(44.dp))
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(
                track.title.ifBlank { "未知曲目" },
                style = MaterialTheme.typography.titleMedium,
                fontSize = 15.sp,
                color = if (active) colors.accentText else colors.text,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Row(
                horizontalArrangement = Arrangement.spacedBy(6.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    track.artist.ifBlank { track.artists.joinToString(" / ") }.ifBlank { "未知歌手" },
                    style = MaterialTheme.typography.bodySmall,
                    color = colors.secondary,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f, fill = false),
                )
                if (cleanReason != null) {
                    Text(
                        cleanReason,
                        Modifier.clip(RoundedCornerShape(4.dp))
                            .background(colors.surfaceWell)
                            .padding(horizontal = 6.dp, vertical = 1.dp),
                        style = MaterialTheme.typography.labelMedium,
                        color = colors.secondary,
                        maxLines = 1,
                    )
                }
            }
        }
        if (duration.isNotEmpty()) {
            Text(
                duration,
                style = PrunerTypography.number.copy(fontSize = 12.sp),
                color = colors.secondary,
            )
        }
        trailing()
    }
}

@Composable
fun prunerTextFieldColors() =
    OutlinedTextFieldDefaults.colors(
        focusedContainerColor = colors.surface,
        unfocusedContainerColor = colors.surface,
        focusedBorderColor = colors.accentText,
        unfocusedBorderColor = colors.borderMedium,
        focusedLabelColor = colors.accentText,
        unfocusedLabelColor = colors.secondary,
        cursorColor = colors.accentText,
        focusedTextColor = colors.text,
        unfocusedTextColor = colors.text,
    )

@Composable
fun LoginQr(url: String) {
    val matrix =
        remember(url) {
            QRCodeWriter()
                .encode(url, BarcodeFormat.QR_CODE, 1, 1, mapOf(EncodeHintType.MARGIN to 4))
        }
    Canvas(Modifier.fillMaxWidth().aspectRatio(1f).background(Color.White)) {
        drawRect(Color.White)
        val unit = (size.minDimension / matrix.width).toInt()
        val origin = ((size.minDimension - unit * matrix.width) / 2).toInt()
        for (y in 0 until matrix.height) for (x in 0 until matrix.width) if (matrix[x, y])
            drawRect(
                Color.Black,
                Offset((origin + x * unit).toFloat(), (origin + y * unit).toFloat()),
                Size(unit.toFloat(), unit.toFloat()),
            )
    }
}
