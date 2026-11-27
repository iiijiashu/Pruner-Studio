@file:OptIn(kotlinx.serialization.ExperimentalSerializationApi::class)

package com.prunerstudio.mobile.core.model

import kotlinx.serialization.*
import kotlinx.serialization.json.*

val wire = Json {
    ignoreUnknownKeys = true
    explicitNulls = false
    encodeDefaults = true
}

@Serializable
data class Track(
    val id: Long? = null,
    val song_id: Long? = null,
    val title: String = "",
    val artists: List<String> = emptyList(),
    val artist: String = "",
    val album: String = "",
    val provider: String = "",
    val external_id: String? = null,
    val track_key: String? = null,
    val duration: Double = 0.0,
    val duration_ms: Long = 0,
    val cover_url: String = "",
    val source_playlist_id: JsonPrimitive? = null,
    val file_id: String? = null,
    val stream_url: String? = null,
    val play_count: Int = 0,
    val completed_count: Int = 0,
    val total_listen_ms: Long = 0,
    val last_played_at: String? = null,
    val availability: String? = null,
    val chorus_start: Int? = null,
    val chorus_end: Int? = null,
    val fee: Int? = null,
    val privilege_st: Int? = null,
    val max_br_level: String = "",
    val is_cloud: Boolean? = null,
    val source_playlist_name: String = "",
    val artist_ids: List<Long> = emptyList(),
    val liked: Boolean = false,
    val duration_unit: String? = null,
    val source: String? = null,
    val album_id: Long? = null,
    val source_id: String? = null,
    val desktop_scope: String? = null,
) {
    fun durationSeconds(): Double {
        if (duration_ms > 0) return duration_ms / 1000.0
        if (!duration.isFinite() || duration <= 0) return 0.0
        if (duration_unit == "sec") return duration
        val millisecondProviders = setOf("netease", "local")
        val milliseconds =
            duration_unit == "ms" ||
                provider.trim().lowercase() in millisecondProviders ||
                source?.trim()?.lowercase() in millisecondProviders ||
                track_key?.substringBefore(':') in millisecondProviders ||
                fee != null ||
                privilege_st != null ||
                is_cloud != null ||
                duration > 3600
        return if (milliseconds) duration / 1000.0 else duration
    }

    fun effectiveProvider() = provider.ifBlank { "netease" }

    fun providerFamily(): String = effectiveProvider().lowercase().let {
        if (it.startsWith("resolver:") || it.startsWith("resolver_v2:")) it.substringBefore(':') else it
    }

    internal fun canonicalForLan(): Track {
        val legacy = effectiveProvider().lowercase()
        val id = external_id?.takeIf(String::isNotBlank) ?: return this
        if (legacy !in setOf("resolver", "resolver_v2")) return this
        val oldKey = track_key.orEmpty()
        val source = source_id?.takeUnless { it == "lan_desktop" }
            ?: oldKey.removePrefix("$legacy:").removeSuffix(":$id").takeIf {
                oldKey == "$legacy:$it:$id"
            } ?: return this
        if (!validResolverSourceId(source)) return this
        val composite = "$legacy:${source.lowercase()}"
        return copy(provider = composite, track_key = "$composite:$id")
    }

    fun displayProviderLabel(): String =
        when (val p = effectiveProvider().lowercase()) {
            "local" -> "桌面无损"
            "device" -> "本机音频"
            "netease" -> "网易云音乐"
            "subsonic", "opensubsonic" -> "OpenSubsonic"
            "webdav" -> "WebDAV"
            "resolver", "resolver_v2" -> "HTTP Resolver"
            "bilibili" -> "Bilibili"
            "qq" -> "QQ 音乐"
            "kugou", "kugou_lite" -> "酷狗音乐"
            "kuwo" -> "酷我音乐"
            "migu" -> "咪咕音乐"
            "youtube" -> "YouTube"
            "apple_music" -> "Apple Music"
            "custom", "custom_endpoint" -> "HTTP Resolver"
            else ->
                when {
                    p.startsWith("resolver:") -> "HTTP Resolver"
                    p.startsWith("gdstudio") -> "GDStudio"
                    else -> effectiveProvider()
                }
        }

    fun identity() = track_key ?: "${effectiveProvider()}:${external_id ?: song_id ?: id}"

    fun isStubTrack(): Boolean {
        val cleanTitle = title.trim()
        val unavailableFlag =
            availability?.lowercase() in setOf("unavailable", "delisted", "removed", "copyright")
        val looksLikeRawIdTitle =
            cleanTitle.isEmpty() ||
                cleanTitle.matches(Regex("^歌曲\\s*#?\\d+$")) ||
                cleanTitle.matches(Regex("^#?\\d{4,}$"))
        return unavailableFlag || looksLikeRawIdTitle
    }
}

internal fun validResolverSourceId(id: String): Boolean =
    id.lowercase().matches(Regex("[a-z0-9_.-]{1,48}")) && id != "." && id != ".."

internal fun lanProviderFromKey(key: String): String {
    val parts = key.split(':', limit = 3)
    return if (parts.size == 3 && parts[0] in setOf("gdstudio", "resolver", "resolver_v2")) {
        "${parts[0]}:${parts[1]}"
    } else parts[0]
}

const val SMART_RADIO_EXPLORE_MIN = 0.0f
const val SMART_RADIO_EXPLORE_MAX = 1.0f
const val SMART_RADIO_EXPLORE_DEFAULT = 0.5f

fun sanitizeExploreRatio(raw: Float): Float =
    if (!raw.isFinite()) SMART_RADIO_EXPLORE_DEFAULT
    else raw.coerceIn(SMART_RADIO_EXPLORE_MIN, SMART_RADIO_EXPLORE_MAX)

fun sanitizeExploreRatio(raw: Double): Float =
    if (!raw.isFinite()) SMART_RADIO_EXPLORE_DEFAULT
    else raw.toFloat().coerceIn(SMART_RADIO_EXPLORE_MIN, SMART_RADIO_EXPLORE_MAX)

fun exploreRatioPercent(ratio: Float): Int =
    kotlin.math.round(sanitizeExploreRatio(ratio) * 100f).toInt().coerceIn(0, 100)

fun exploreRatioLabel(ratio: Float): String =
    when (exploreRatioPercent(ratio)) {
        0 -> "纯熟歌回味"
        in 1..29 -> "偏爱熟歌"
        in 30..69 -> "熟歌与新声均衡"
        in 70..99 -> "偏爱探索新声"
        else -> "纯新歌探索"
    }

@Serializable
data class SmartRadioSettings(
    val explore_ratio: Double = SMART_RADIO_EXPLORE_DEFAULT.toDouble(),
    val smart_radio_explore_ratio: Double = SMART_RADIO_EXPLORE_DEFAULT.toDouble(),
) {
    fun normalizedRatio(): Float = sanitizeExploreRatio(smart_radio_explore_ratio)
}

typealias PlayHistoryItem = Track

@Serializable
data class PlaylistSummary(
    val id: JsonPrimitive,
    val name: String = "",
    val kind: String = "",
    val track_count: Int = 0,
    val privacy: String = "",
    val cover_url: String = "",
) {
    fun displayName() = name.ifBlank { "未命名歌单 #${id.content}" }
}

@Serializable
data class PlayEventPayload(
    val event_id: String,
    val track: Track,
    val listen_ms: Long,
    val completed: Boolean,
)

@Serializable
data class FeedbackItem(
    val action: String,
    val track_key: String,
    val song_id: Long = 0,
    val context: String = "",
    val client_event_id: String? = null,
    val created_at: String? = null,
    val id: Long? = null,
)

@Serializable
data class RuleClause(
    val field: String = "",
    val operator: String = "",
    @SerialName("value") val operand: JsonPrimitive? = null,
) {
    constructor(
        field: String,
        operator: String,
        value: String,
    ) : this(field, operator, JsonPrimitive(value))

    val value: String?
        get() {
            val raw = operand?.contentOrNull ?: return null
            if (operand.isString || operand.booleanOrNull != null) return raw
            val number = operand.doubleOrNull?.takeIf { it.isFinite() } ?: return null
            val decimal = java.math.BigDecimal.valueOf(number).stripTrailingZeros()
            val exponent = decimal.precision() - decimal.scale() - 1
            if (exponent in -6..20) return decimal.toPlainString()
            val sign = if (exponent >= 0) "+" else ""
            return "${decimal.movePointLeft(exponent).toPlainString()}e$sign$exponent"
        }
}

@Serializable
data class PruningRule(
    val id: String = "",
    val name: String = "",
    @EncodeDefault val enabled: Boolean = true,
    @EncodeDefault val action: String = "prune",
    @EncodeDefault val match_mode: String = "all",
    val match_clauses: List<RuleClause> = emptyList(),
    val output_playlist_name: String = "",
)

@Serializable
enum class PlayMode {
    order,
    loop,
    @SerialName("single") repeat_one,
    shuffle,
    fm,
    intelligence,
    smart_radio,
}

@Serializable
enum class AudioQualityLevel {
    jymaster,
    sky,
    jyeffect,
    hires,
    lossless,
    exhigh,
    higher,
    standard;

    fun displayLabel(): String =
        when (this) {
            jymaster -> "超清母带"
            sky -> "沉浸环绕"
            jyeffect -> "高清环绕"
            hires -> "Hi-Res"
            lossless -> "无损 FLAC"
            exhigh -> "极高音质"
            higher -> "较高音质"
            standard -> "标准音质"
        }
}

@Serializable
enum class ExternalSourceKind(val label: String) {
    RESOLVER_V2("HTTP Resolver v2"),
    OPENSUBSONIC("OpenSubsonic"),
    WEBDAV("WebDAV"),
}

@Serializable
data class ExternalSourceConfig(
    val id: String,
    val name: String,
    val kind: ExternalSourceKind,
    val baseUrl: String,
    val username: String = "",
    val enabled: Boolean = true,
    val allowLanHttp: Boolean = false,
)

@Serializable
data class PlaybackSessionSnapshot(
    val current_track: Track? = null,
    val queue: List<Track> = emptyList(),
    val position_sec: Double = 0.0,
    val duration_sec: Double = 0.0,
    val is_playing: Boolean = false,
    val queue_index: Int = -1,
    val play_mode: PlayMode = PlayMode.order,
    val volume: Double = 1.0,
    val muted: Boolean = false,
    val playback_rate: Double = 1.0,
    val updated_at: String? = null,
    val active_tab: String? = null,
)

@Serializable
data class HandoffSnapshot(
    val track: Track? = null,
    val queue: List<Track> = emptyList(),
    val queue_index: Int = -1,
    val play_mode: PlayMode = PlayMode.order,
    val position_sec: Double = 0.0,
    val duration_sec: Double = 0.0,
    val playback_rate: Double = 1.0,
    val is_playing: Boolean = false,
    val handoff_id: String? = null,
    val source_device_id: String? = null,
    val source_device_name: String? = null,
    val source_platform: String? = null,
    val account_id: String? = null,
    val updated_at: String? = null,
    val explicit_handoff: Boolean = false,
    val consumed_by: List<String> = emptyList(),
)

@Serializable
data class LanLocalFile(
    val file_id: String,
    val stream_url: String,
    val title: String = "",
    val artist: String = "",
    val album: String = "",
    val duration_ms: Long = 0,
    val format: String = "",
    val bitrate: Long = 0,
    val mtime: Double = 0.0,
    val cover_url: String = "",
)

@Serializable
data class LocalPage(
    val files: List<LanLocalFile>,
    val total: Int,
    val limit: Int,
    val offset: Int,
    val has_more: Boolean,
)

@Serializable
data class LanMultiSearchResponse(
    val query: String = "",
    val offset: Int = 0,
    val limit: Int = 30,
    val has_more: Boolean = false,
    val channels_queried: List<String> = emptyList(),
    val tracks: List<Track> = emptyList(),
    val channel_errors: Map<String, String> = emptyMap(),
)

@Serializable
data class LanServerInfo(
    val device_name: String,
    val cert_fingerprint: String,
    val protocol_version: Int,
    val pairing_active: Boolean,
    val tls: Boolean,
    val spki_pin: String? = null,
    val spki_fingerprint: String? = null,
)

@Serializable
data class PairingClaimResponse(
    val device_id: String,
    val token: String,
    val server_name: String,
    val protocol_version: Int,
    val account: JsonObject,
    val spki_pin: String? = null,
    val spki_fingerprint: String? = null,
) {
    override fun toString() =
        "PairingClaimResponse(server_name=$server_name, protocol_version=$protocol_version)"
}

@Serializable
data class PairedServerConfig(
    val host: String,
    val port: Int,
    val fingerprint: String,
    val token: String,
    val deviceId: String,
    val accountId: String,
    val name: String = "",
    val spki_pin: String? = null,
) {
    override fun toString() = "PairedServerConfig($host:$port)"
}

@Serializable data class SyncWatermarks(val since: String? = null)

@Serializable
data class SyncPullRequest(
    val since: String?,
    val include_catalog: Boolean,
    val include_local: Boolean,
    val history_limit: Int,
    val feedback_limit: Int,
    val account_id: String? = null,
    val until: String? = null,
    val history_cursor: JsonObject? = null,
    val feedback_cursor: JsonObject? = null,
)

@Serializable
data class SyncPullResponse(
    val sync_until: String,
    val account_id: String,
    val server_time: String,
    val history_has_more: Boolean,
    val feedback_has_more: Boolean,
    val history_next_cursor: JsonObject? = null,
    val feedback_next_cursor: JsonObject? = null,
    val play_history: List<Track>,
    val feedback: List<FeedbackItem>,
    val rules: List<PruningRule> = emptyList(),
    val playback_session: PlaybackSessionSnapshot? = null,
    val playlists: List<PlaylistSummary>? = null,
    val songs: List<Track>? = null,
    val local_library: List<LanLocalFile>? = null,
    val smart_radio_explore_ratio: Double? = null,
)

@Serializable
data class SyncPushRequest(
    val account_id: String,
    val play_events: List<PlayEventPayload> = emptyList(),
    val feedback: List<FeedbackItem> = emptyList(),
    val playback_session: PlaybackSessionSnapshot? = null,
    val force_session: Boolean = false,
    val rules: List<PruningRule> = emptyList(),
)

@Serializable
data class SyncPushResponse(
    val accepted_events: Int,
    val accepted_feedback: Int,
    val session_updated: Boolean,
    val server_time: String,
)

@Serializable data class LyricWord(val startMs: Long, val durationMs: Long, val text: String)

@Serializable
data class LyricLine(
    val startMs: Long,
    val text: String,
    val words: List<LyricWord> = emptyList(),
    val translation: String? = null,
    val romaji: String? = null,
)

@Serializable data class ParsedLyrics(val lines: List<LyricLine>)
