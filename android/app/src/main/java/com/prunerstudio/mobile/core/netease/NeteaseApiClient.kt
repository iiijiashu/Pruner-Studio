package com.prunerstudio.mobile.core.netease

import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.awaitResponse
import java.io.IOException
import kotlinx.coroutines.*
import kotlinx.serialization.json.*
import okhttp3.*
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

val TRUSTED_NETEASE_HOSTS =
    setOf("music.163.com", "interface.music.163.com", "interface3.music.163.com")

const val NETEASE_DESKTOP_USER_AGENT =
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

data class QrPollResult(val code: Int, val candidateCookie: String?) {
    override fun toString() = "QrPollResult(code=$code)"
}

class NeteaseApiClient internal constructor(
    private val store: SecureCredentialStore,
    private val client: Call.Factory,
    val rateLimiter: NeteaseRateLimiter = NeteaseRateLimiter(),
) {
    constructor(store: SecureCredentialStore) : this(
        store,
        OkHttpClient.Builder()
            .followRedirects(false)
            .followSslRedirects(false)
            .retryOnConnectionFailure(false)
            .addInterceptor { chain ->
                val url = chain.request().url
                require(url.isHttps && url.host in TRUSTED_NETEASE_HOSTS && url.port == 443)
                chain.proceed(chain.request())
            }
            .build(),
    )

    private suspend fun post(
        path: String,
        fields: Map<String, Any> = emptyMap(),
        host: String = "music.163.com",
        cookie: String? = null,
        onLoginCookie: ((String) -> Unit)? = null,
        encrypted: Boolean = false,
        get: Boolean = false,
        isWrite: Boolean = false,
        session: AuthWriteSession? = null,
    ): JsonObject =
        withContext(Dispatchers.IO) {
            session?.requireCurrent()
            if (isWrite) {
                if (session == null) rateLimiter.acquireWriteSlot() else session.awaitPacing(rateLimiter::acquireWriteSlot)
            } else {
                rateLimiter.checkCircuit()
            }
            val resolvedCookie = session?.cookie ?: cookie ?: store.cookie
            val persistedDeviceId = store.getOrCreateDeviceId().also(NeteaseCrypto::setDefaultDeviceId)
            val form = FormBody.Builder()
            fields.forEach { (k, v) -> form.add(k, v.toString()) }
            resolvedCookie
                ?.split(';')
                ?.map(String::trim)
                ?.find { it.startsWith("__csrf=") }
                ?.let { if (!encrypted) form.add("csrf_token", it.substringAfter('=')) }
            val cookieHeader =
                resolvedCookie?.takeIf { it.isNotBlank() }?.let { raw ->
                    if ("deviceId=" in raw) raw else "$raw; deviceId=$persistedDeviceId"
                } ?: "os=pc; appver=3.1.3.203419; deviceId=$persistedDeviceId"
            val request =
                Request.Builder()
                    .url("https://$host$path")
                    .header("User-Agent", NETEASE_DESKTOP_USER_AGENT)
                    .header("Referer", "https://music.163.com/")
                    .header("Cookie", cookieHeader)
                    .header("Origin", "https://music.163.com")
                    .header("Accept", "application/json")
                    .apply {
                        if (get)
                            url(
                                HttpUrl.Builder()
                                    .scheme("https")
                                    .host(host)
                                    .encodedPath(path)
                                    .apply {
                                        fields.forEach { (k, v) ->
                                            addQueryParameter(k, v.toString())
                                        }
                                    }
                                    .build()
                            )
                        else post(form.build())
                    }
                    .build()

            var attempt = 0
            while (attempt <= rateLimiter.config.readRetryMaxAttempts) {
                rateLimiter.checkCircuit()
                session?.requireCurrent()
                var shouldRetryRead = false
                val call = client.newCall(request)
                val invalidation = session?.onInvalidation(call::cancel)
                val parsed: JsonObject? = try {
                    call.awaitResponse<JsonObject?> { response ->
                        val httpCode = response.code
                        if (httpCode == 429) {
                            rateLimiter.recordRateLimit(
                                httpCode, null, retryAfterSeconds(response.header("Retry-After")), read = !isWrite,
                            )
                            if (!isWrite) {
                                shouldRetryRead = true
                                return@awaitResponse null
                            }
                            throw AudioResolutionException(AudioErrorCategory.RATE_LIMITED)
                        }
                        if (!response.isSuccessful) throw IOException("NetEase HTTP $httpCode")
                        val source = requireNotNull(response.body).source()
                        if (source.request(4_194_305)) throw IOException("NetEase response too large")
                        val bytes = source.readByteArray()
                        val rawText = bytes.decodeToString()
                        val obj =
                            try {
                                wire.parseToJsonElement(rawText).jsonObject
                            } catch (e: IllegalArgumentException) {
                                if (!encrypted)
                                    throw AudioResolutionException(
                                        AudioErrorCategory.PARSE_ERROR,
                                        "NetEase JSON parse failed",
                                        e,
                                    )
                                val trimmed = rawText.trim()
                                val decoded =
                                    try {
                                        if (
                                            trimmed.isNotEmpty() &&
                                                trimmed.length % 2 == 0 &&
                                                trimmed.all { it in "0123456789ABCDEFabcdef" }
                                        )
                                            NeteaseCrypto.eapiDecrypt(trimmed)
                                        else NeteaseCrypto.eapiDecrypt(bytes)
                                    } catch (sec: Exception) {
                                        throw AudioResolutionException(
                                            AudioErrorCategory.PARSE_ERROR,
                                            "NetEase EAPI decrypt failed",
                                            sec,
                                        )
                                    }
                                try {
                                    wire.parseToJsonElement(decoded.decodeToString()).jsonObject
                                } catch (parse: IllegalArgumentException) {
                                    throw AudioResolutionException(
                                        AudioErrorCategory.PARSE_ERROR,
                                        "NetEase decrypted JSON parse failed",
                                        parse,
                                    )
                                }
                            }
                        val code = obj["code"]?.jsonPrimitive?.intOrNull
                        if (rateLimiter.isRiskControlOrRateLimit(httpCode, code)) {
                            rateLimiter.recordRateLimit(httpCode, code)
                            rateLimiter.checkCircuit()
                        }
                        if (onLoginCookie != null && code == 803) {
                            val pairs =
                                Cookie.parseAll(request.url, response.headers).filter {
                                    it.matches(request.url) || it.path == "/"
                                }
                            require(pairs.any { it.name == "MUSIC_U" && it.value.isNotBlank() })
                            onLoginCookie(pairs.joinToString("; ") { "${it.name}=${it.value}" })
                        } else if (code == 301 || code == 401)
                            throw AudioResolutionException(
                                AudioErrorCategory.LOGIN_REQUIRED,
                                "NetEase response $code",
                            )
                        else if (code != 200 && !(onLoginCookie != null && code in 800..802))
                            throw IOException("NetEase response $code")
                        rateLimiter.recordSuccess()
                        obj
                    }
                } finally {
                    invalidation?.dispose()
                }
                if (parsed != null) return@withContext parsed
                if (shouldRetryRead && rateLimiter.backoffForReadRetry(attempt)) {
                    attempt++
                    continue
                }
                rateLimiter.checkCircuit()
                throw AudioResolutionException(AudioErrorCategory.RATE_LIMITED)
            }
            throw AudioResolutionException(AudioErrorCategory.RATE_LIMITED)
        }

    suspend fun createQrKey(): String =
        post("/api/login/qrcode/unikey", mapOf("type" to 1)).let {
            (it["unikey"] ?: it["data"]?.jsonObject?.get("unikey"))!!.jsonPrimitive.content
        }

    suspend fun checkQrStatus(key: String): QrPollResult {
        var cookie: String? = null
        val result = post(
            "/api/login/qrcode/client/login",
            mapOf("key" to key, "type" to 1, "timestamp" to System.currentTimeMillis()),
            onLoginCookie = { cookie = it },
        )
        return QrPollResult(result.getValue("code").jsonPrimitive.int, cookie)
    }

    private fun retryAfterSeconds(value: String?): Long? {
        val header = value?.trim() ?: return null
        return header.toLongOrNull()?.takeIf { it >= 0 } ?: runCatching {
            val until = java.time.ZonedDateTime.parse(header, java.time.format.DateTimeFormatter.RFC_1123_DATE_TIME)
            val millis = java.time.Duration.between(java.time.Instant.now(), until.toInstant()).toMillis()
            (millis.coerceAtLeast(0) + 999) / 1000
        }.getOrNull()
    }

    suspend fun fetchAccount(session: AuthWriteSession? = null) =
        post("/api/nuser/account/get", get = true, session = session)

    suspend fun fetchUserPlaylists(userId: Long, limit: Int = 100, offset: Int = 0) =
        post(
            "/api/user/playlist",
            mapOf("uid" to userId, "limit" to limit, "offset" to offset),
            get = true,
        )

    suspend fun fetchPlaylistTracks(id: Long, strict: Boolean = false, session: AuthWriteSession? = null): List<Track> {
        val playlist =
            post("/api/v6/playlist/detail", mapOf("id" to id, "n" to 100000, "s" to 0), get = true, session = session)
                .getValue("playlist")
                .jsonObject
        fun JsonElement.id() = jsonObject.getValue("id").jsonPrimitive.long
        val rawIds = playlist["trackIds"]?.jsonArray.orEmpty().map { it.id() }
        val ids =
            if (strict) {
                require(
                    rawIds.all { it > 0 } &&
                        rawIds.distinct().size == rawIds.size &&
                        rawIds.size == (playlist["trackCount"]?.jsonPrimitive?.int ?: rawIds.size)
                )
                rawIds
            } else {
                rawIds.filter { it > 0 }.distinct()
            }
        val byId = playlist["tracks"]?.jsonArray.orEmpty().associateBy { it.id() }.toMutableMap()
        for (batch in ids.filterNot(byId::containsKey).chunked(500)) {
            val songs =
                post(
                    "/api/v3/song/detail",
                    mapOf(
                        "ids" to batch.toString(),
                        "c" to JsonArray(batch.map { buildJsonObject { put("id", it) } }),
                    ),
                    session = session,
                )
            songs["songs"]?.jsonArray.orEmpty().forEach { byId[it.id()] = it }
        }
        return if (strict) {
            ids.map { songId ->
                parseTrack(byId.getValue(songId).jsonObject)
                    .copy(source_playlist_id = JsonPrimitive(id))
            }
        } else {
            ids.mapNotNull { songId ->
                byId[songId]?.jsonObject?.let {
                    parseTrack(it).copy(source_playlist_id = JsonPrimitive(id))
                }
            }
        }
    }

    fun parseTrack(song: JsonObject): Track {
        val id = song.getValue("id").jsonPrimitive.long
        val artists = (song["ar"] ?: song["artists"])?.jsonArray.orEmpty().map { it.jsonObject }
        val album = (song["al"] ?: song["album"]) as? JsonObject
        val duration = (song["dt"] ?: song["duration"])?.jsonPrimitive?.longOrNull ?: 0
        return Track(
            id = id,
            song_id = id,
            provider = "netease",
            external_id = id.toString(),
            track_key = "netease:$id",
            title = song.getValue("name").jsonPrimitive.content,
            artists = artists.mapNotNull { it["name"]?.jsonPrimitive?.contentOrNull },
            artist_ids = artists.mapNotNull { it["id"]?.jsonPrimitive?.longOrNull },
            album = album?.get("name")?.jsonPrimitive?.contentOrNull.orEmpty(),
            duration_ms = duration,
            duration = duration.toDouble(),
            cover_url = album?.get("picUrl")?.jsonPrimitive?.contentOrNull.orEmpty(),
            fee = song["fee"]?.jsonPrimitive?.intOrNull,
        )
    }

    suspend fun resolveAudioUrl(
        id: Long,
        level: AudioQualityLevel = AudioQualityLevel.jymaster,
    ): HttpUrl? =
        try {
            resolveAudioUrlOrThrow(id, level)
        } catch (_: AudioResolutionException) {
            null
        }

    suspend fun resolveAudioUrlOrThrow(
        id: Long,
        level: AudioQualityLevel = AudioQualityLevel.jymaster,
    ): HttpUrl {
        val (cookie, persistedDeviceId) =
            withContext(Dispatchers.IO) {
                val c = store.cookie
                val d = store.getOrCreateDeviceId().also(NeteaseCrypto::setDefaultDeviceId)
                c to d
            }
        val levels =
            listOf(level) +
                listOf(
                        AudioQualityLevel.hires,
                        AudioQualityLevel.lossless,
                        AudioQualityLevel.exhigh,
                        AudioQualityLevel.standard,
                    )
                    .filter { it.ordinal > level.ordinal }
        var sawFreeTrial = false
        var sawResponse = false
        var lastError: IOException? = null
        for (q in levels) {
            val header = NeteaseCrypto.buildEapiHeader(cookie, deviceId = persistedDeviceId)
            val payload = NeteaseCrypto.buildPlayerUrlV1Payload(listOf(id), q.name, cookie, header)
            val fields =
                NeteaseCrypto.eapiEncrypt("/api/song/enhance/player/url/v1", payload.toString())
            val result =
                try {
                    post(
                        "/eapi/song/enhance/player/url/v1",
                        fields,
                        "interface.music.163.com",
                        NeteaseCrypto.buildEapiCookie(cookie, header),
                        encrypted = true,
                    )
                } catch (e: AudioResolutionException) {
                    if (
                        e.category == AudioErrorCategory.LOGIN_REQUIRED ||
                            e.category == AudioErrorCategory.RATE_LIMITED
                    )
                        throw e
                    lastError = e
                    continue
                } catch (e: IOException) {
                    lastError = e
                    continue
                }
            sawResponse = true
            val item = result["data"]?.jsonArray?.firstOrNull()?.jsonObject ?: continue
            if (item["freeTrialInfo"]?.let { it != JsonNull } == true) {
                sawFreeTrial = true
                continue
            }
            val url = item["url"]?.jsonPrimitive?.contentOrNull?.toHttpUrlOrNull() ?: continue
            if (url.host.endsWith(".126.net") || url.host.endsWith(".music.163.com"))
                return url.newBuilder().scheme("https").port(443).build()
        }
        if (sawFreeTrial) throw AudioResolutionException(AudioErrorCategory.VIP_OR_TRIAL_ONLY)
        if (!sawResponse && lastError != null) {
            if (lastError is AudioResolutionException) throw lastError
            throw AudioResolutionException(
                AudioErrorCategory.NETWORK_ERROR,
                AudioErrorCategory.NETWORK_ERROR.userMessage,
                lastError,
            )
        }
        if (cookie.isNullOrBlank()) {
            throw AudioResolutionException(
                AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE,
                "该曲目暂无可用音源，登录网易云音乐账号可能解锁更多音质",
            )
        }
        throw AudioResolutionException(AudioErrorCategory.COPYRIGHT_OR_UNAVAILABLE)
    }

    suspend fun fetchLyrics(id: Long) =
        post(
            "/api/song/lyric/v1",
            mapOf(
                "id" to id,
                "tv" to 0,
                "lv" to 0,
                "rv" to 0,
                "kv" to 0,
                "yv" to 0,
                "ytv" to 0,
                "yrv" to 0,
            ),
            "interface.music.163.com",
        )

    suspend fun searchSongs(query: String, limit: Int = 30, offset: Int = 0) =
        post(
            "/api/search/get/web",
            mapOf("s" to query, "type" to 1, "limit" to limit, "offset" to offset),
            get = true,
        )

    suspend fun fetchDailyRecommend() = post("/api/v3/discovery/recommend/songs")

    suspend fun like(id: Long, liked: Boolean, session: AuthWriteSession? = null) =
        post(
            "/api/radio/like",
            mapOf("trackId" to id, "like" to liked, "time" to 25),
            get = true,
            isWrite = true,
            session = session,
        )

    /**
     * Paced micro-batched unlike operation. Stops immediately if risk-control circuit trips
     * so NetEase accounts are never hammered with unthrottled bursts.
     */
    suspend fun unlikeTracksBatch(
        ids: List<Long>,
        session: AuthWriteSession? = null,
        onProgress: ((completed: Int, total: Int) -> Unit)? = null,
    ): BatchWriteResult {
        val unique = ids.distinct()
        if (unique.isEmpty()) return BatchWriteResult(emptyList(), emptyList())
        val succeeded = mutableListOf<Long>()
        val failed = mutableListOf<Long>()
        val batchSize = rateLimiter.config.microBatchSize.coerceAtLeast(1)
        unique.forEachIndexed { index, trackId ->
            if (index > 0 && index % batchSize == 0) {
                try {
                    if (session == null) rateLimiter.pauseBetweenMicroBatches()
                    else session.awaitPacing(rateLimiter::pauseBetweenMicroBatches)
                } catch (e: IOException) {
                    failed.addAll(unique.subList(index, unique.size))
                    return BatchWriteResult(
                        succeededIds = succeeded,
                        failedIds = failed,
                        circuitTripped = rateLimiter.isCircuitOpen(),
                        errorMessage = e.message,
                    )
                }
            }
            try {
                like(trackId, false, session)
                succeeded.add(trackId)
                onProgress?.invoke(succeeded.size, unique.size)
            } catch (e: AudioResolutionException) {
                failed.addAll(unique.subList(index, unique.size))
                return BatchWriteResult(
                    succeededIds = succeeded,
                    failedIds = failed,
                    circuitTripped = e.category == AudioErrorCategory.RATE_LIMITED,
                    errorMessage = e.message,
                )
            } catch (e: IOException) {
                failed.addAll(unique.subList(index, unique.size))
                return BatchWriteResult(
                    succeededIds = succeeded,
                    failedIds = failed,
                    circuitTripped = rateLimiter.isCircuitOpen(),
                    errorMessage = e.message ?: "网络请求异常，已保留剩余曲目",
                )
            }
        }
        return BatchWriteResult(succeededIds = succeeded, failedIds = failed)
    }

    /**
     * Paced micro-batched playlist track add/remove returning partial success/failure cleanly
     * so risk-control circuit trips mid-batch preserve completed chunks and never drop state.
     */
    suspend fun changePlaylistTracksBatch(
        id: Long,
        tracks: List<Long>,
        add: Boolean,
        chunkSize: Int = 25,
        session: AuthWriteSession? = null,
        onProgress: ((completed: Int, total: Int) -> Unit)? = null,
    ): BatchWriteResult {
        val unique = tracks.distinct()
        if (unique.isEmpty()) return BatchWriteResult(emptyList(), emptyList())
        val batches = unique.chunked(chunkSize.coerceIn(1, 50))
        val succeeded = mutableListOf<Long>()
        val failed = mutableListOf<Long>()
        batches.forEachIndexed { idx, batch ->
            if (idx > 0) {
                try {
                    if (session == null) rateLimiter.pauseBetweenMicroBatches()
                    else session.awaitPacing(rateLimiter::pauseBetweenMicroBatches)
                } catch (e: IOException) {
                    failed.addAll(batches.subList(idx, batches.size).flatten())
                    return BatchWriteResult(
                        succeededIds = succeeded,
                        failedIds = failed,
                        circuitTripped = rateLimiter.isCircuitOpen(),
                        errorMessage = e.message,
                    )
                }
            }
            try {
                post(
                    "/api/playlist/manipulate/tracks",
                    mapOf(
                        "op" to if (add) "add" else "del",
                        "pid" to id,
                        "trackIds" to batch,
                        "tracks" to batch,
                    ),
                    isWrite = true,
                    session = session,
                )
                succeeded.addAll(batch)
                onProgress?.invoke(succeeded.size, unique.size)
            } catch (e: AudioResolutionException) {
                failed.addAll(batches.subList(idx, batches.size).flatten())
                return BatchWriteResult(
                    succeededIds = succeeded,
                    failedIds = failed,
                    circuitTripped = e.category == AudioErrorCategory.RATE_LIMITED,
                    errorMessage = e.message,
                )
            } catch (e: IOException) {
                failed.addAll(batches.subList(idx, batches.size).flatten())
                return BatchWriteResult(
                    succeededIds = succeeded,
                    failedIds = failed,
                    circuitTripped = rateLimiter.isCircuitOpen(),
                    errorMessage = e.message ?: "歌单写入中断，已保留未完成曲目",
                )
            }
        }
        return BatchWriteResult(succeededIds = succeeded, failedIds = failed)
    }

    suspend fun changePlaylistTracks(
        id: Long,
        tracks: List<Long>,
        add: Boolean,
        chunkSize: Int = 25,
    ): JsonObject {
        val unique = tracks.distinct()
        require(unique.isNotEmpty())
        val batches = unique.chunked(chunkSize.coerceIn(1, 50))
        var lastResponse: JsonObject? = null
        batches.forEachIndexed { idx, batch ->
            if (idx > 0) rateLimiter.pauseBetweenMicroBatches()
            lastResponse =
                post(
                    "/api/playlist/manipulate/tracks",
                    mapOf(
                        "op" to if (add) "add" else "del",
                        "pid" to id,
                        "trackIds" to batch,
                        "tracks" to batch,
                    ),
                    isWrite = true,
                )
        }
        return requireNotNull(lastResponse)
    }
}
