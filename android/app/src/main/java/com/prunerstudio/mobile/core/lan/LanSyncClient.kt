package com.prunerstudio.mobile.core.lan

import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.*
import com.prunerstudio.mobile.core.awaitResponse
import java.io.IOException
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.*
import kotlinx.serialization.*
import kotlinx.serialization.json.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody

internal fun FeedbackItem.canSync() = track_key.substringBefore(':') != "device"

internal fun Track.canSync() = provider != "device" && identity().substringBefore(':') != "device"

class LanException(val status: Int, val code: String) : IOException("LAN $status $code")

class LanSyncClient(
    val config: PairedServerConfig,
    private val store: SecureCredentialStore,
    private val onRevoke: (LanSyncClient) -> Unit = { store.revoke(it.config) },
) {
    companion object {
        fun forPairing(uri: PairingUri, store: SecureCredentialStore) =
            LanSyncClient(
                PairedServerConfig(
                    uri.host,
                    uri.port,
                    uri.fingerprint,
                    "",
                    "",
                    "",
                    spki_pin = uri.spkiPin,
                ),
                store,
            )
    }

    val base =
        HttpUrl.Builder()
            .scheme("https")
            .host(config.host)
            .port(config.port)
            .addPathSegments("api/lan/v1/")
            .build()

    private val streamLock = Any()
    private val openStreams = mutableSetOf<(Call?) -> Unit>()
    private val revocationNotified = AtomicBoolean(false)

    @Volatile
    var isClosed: Boolean = false
        private set

    internal fun trackStream(cancel: (Call?) -> Unit): () -> Unit {
        synchronized(streamLock) {
            if (isClosed) throw IOException("Desktop pairing revoked")
            openStreams.add(cancel)
        }
        return { synchronized(streamLock) { openStreams.remove(cancel) } }
    }

    fun close() {
        close(exemptCall = null)
    }

    private fun close(exemptCall: Call?) {
        val streams = synchronized(streamLock) {
            if (isClosed) return
            isClosed = true
            openStreams.toList().also { openStreams.clear() }
        }
        streams.forEach { cancel -> cancel(exemptCall) }
        val dispatcher = client.dispatcher
        (dispatcher.queuedCalls() + dispatcher.runningCalls()).forEach { call ->
            if (call !== exemptCall) call.cancel()
        }
        client.connectionPool.evictAll()
    }

    fun createAuthenticatedOkHttpClient(
        pinnedFingerprint: String = config.fingerprint,
        bearerToken: String = config.token,
        pinnedSpki: String? = config.spki_pin,
    ): OkHttpClient =
        PinnedTlsFactory(pinnedFingerprint, pinnedSpki)
            .clientBuilder()
            .addInterceptor { chain ->
                val r = chain.request()
                require(
                    r.url.isHttps &&
                        r.url.host == base.host &&
                        r.url.port == base.port &&
                        r.url.encodedPath.startsWith(base.encodedPath)
                )
                val authenticated = bearerToken.isNotEmpty()
                if (authenticated && (isClosed || store.pairing != config))
                    throw IOException("Pairing has changed")
                val result =
                    chain.proceed(
                        if (authenticated)
                            r.newBuilder().header("Authorization", "Bearer $bearerToken").build()
                        else r
                    )
                if (authenticated && result.code == 401) {
                    close(chain.call())
                    // Let JSON consumers handle the denial before tearing down their session.
                    // The completion hook also runs if the caller cancels or already finished.
                    val completion = r.tag(CompletableJob::class.java)
                    if (completion != null) completion.invokeOnCompletion { notifyRevoked() }
                    else notifyRevoked()
                }
                result
            }
            .build()

    val client by lazy { createAuthenticatedOkHttpClient() }
    // Read-only requests can recover a connection that went stale in the pool.
    internal val readClient by lazy { client.newBuilder().retryOnConnectionFailure(true).build() }

    internal suspend fun request(
        path: String,
        body: JsonElement? = null,
        query: Map<String, String> = emptyMap(),
    ): JsonElement {
        val completion = Job()
        try {
            return request(path, body, query, completion)
        } finally {
            completion.complete()
        }
    }

    private suspend fun request(
        path: String,
        body: JsonElement?,
        query: Map<String, String>,
        completion: CompletableJob,
    ): JsonElement =
        withContext(Dispatchers.IO) {
            val url =
                base
                    .newBuilder()
                    .addPathSegments(path)
                    .apply { query.forEach { (k, v) -> addQueryParameter(k, v) } }
                    .build()
            val req =
                Request.Builder()
                    .url(url)
                    .tag(CompletableJob::class.java, completion)
                    .apply {
                        body?.let {
                            post(it.toString().toRequestBody("application/json".toMediaType()))
                        }
                    }
                    .build()
            val http = if (body == null) readClient else client
            http.newCall(req).awaitResponse {
                if (it.code == 401 && config.token.isNotEmpty())
                    throw LanException(401, "LAN_UNAUTHORIZED")
                val source = requireNotNull(it.body).source()
                if (!it.isSuccessful && source.request(65537))
                    throw LanException(it.code, "HTTP_ERROR")
                val raw = source.readUtf8()
                if (!it.isSuccessful)
                    throw LanException(
                        it.code,
                        runCatching {
                                wire
                                    .parseToJsonElement(raw)
                                    .jsonObject["error"]!!
                                    .jsonPrimitive
                                    .content
                            }
                            .getOrDefault("HTTP_ERROR"),
                    )
                wire.parseToJsonElement(raw)
            }
        }

    suspend fun fetchInfo() = wire.decodeFromJsonElement<LanServerInfo>(request("info"))

    suspend fun claimPairing(
        code: String,
        deviceName: String,
        platform: String = "android",
    ): PairedServerConfig {
        val reply =
            wire.decodeFromJsonElement<PairingClaimResponse>(
                request(
                    "pair/claim",
                    buildJsonObject {
                        put("code", code)
                        put("device_name", deviceName)
                        put("platform", platform)
                    },
                )
            )
        require(reply.protocol_version == 1 && reply.token.isNotBlank())
        val resolvedSpki =
            (reply.spki_pin ?: reply.spki_fingerprint)?.let {
                runCatching { PinnedTlsFactory.normalizeSpkiPin(it) }.getOrNull()
            } ?: config.spki_pin
        return config.copy(
                name = reply.server_name,
                token = reply.token,
                deviceId = reply.device_id,
                accountId = reply.account.getValue("account_id").jsonPrimitive.content,
                spki_pin = resolvedSpki,
            )
    }

    suspend fun fetchStatus() = request("status").jsonObject

    suspend fun pullAllDeltas(
        since: String? = null,
        includeCatalog: Boolean = false,
        includeLocal: Boolean = false,
        historyLimit: Int = 100,
        feedbackLimit: Int = 200,
    ): SyncPullResponse {
        val initial =
            SyncPullRequest(
                since,
                includeCatalog,
                includeLocal,
                historyLimit,
                feedbackLimit,
                config.accountId,
            )
        suspend fun page(body: JsonObject) =
            wire.decodeFromJsonElement<SyncPullResponse>(request("sync/pull", body)).also {
                require(it.account_id == config.accountId)
            }
        val first = page(wire.encodeToJsonElement(initial).jsonObject)
        val history = first.play_history.toMutableList()
        val feedback = first.feedback.toMutableList()
        val seen = mutableSetOf<Pair<JsonElement, JsonElement>>()
        var last = first
        while (last.history_has_more || last.feedback_has_more) {
            val h =
                if (last.history_has_more) requireNotNull(last.history_next_cursor) else JsonNull
            val f =
                if (last.feedback_has_more) requireNotNull(last.feedback_next_cursor) else JsonNull
            check(seen.add(h to f)) { "Repeated sync cursor" }
            last =
                page(
                    buildJsonObject {
                        wire
                            .encodeToJsonElement(
                                initial.copy(
                                    include_catalog = false,
                                    include_local = false,
                                    until = first.sync_until,
                                )
                            )
                            .jsonObject
                            .forEach { (k, v) -> put(k, v) }
                        put("history_cursor", h)
                        put("feedback_cursor", f)
                    }
                )
            require(last.sync_until == first.sync_until)
            history.addAll(last.play_history)
            feedback.addAll(last.feedback)
        }
        return first.copy(
            play_history = history,
            feedback = feedback,
            history_has_more = false,
            feedback_has_more = false,
            history_next_cursor = null,
            feedback_next_cursor = null,
        )
    }

    suspend fun pushDeltas(request: SyncPushRequest): SyncPushResponse =
        sendSyncPush(encodeSyncPush(request))

    internal suspend fun sendSyncPush(body: JsonObject): SyncPushResponse =
        wire.decodeFromJsonElement(request("sync/push", body))

    internal fun encodeSyncPush(request: SyncPushRequest): JsonObject {
        require(request.account_id == config.accountId)
        require(request.play_events.all { it.track.canSync() })
        request.playback_session?.let {
            require(
                it.current_track?.provider != "device" &&
                    it.queue.none { track -> track.provider == "device" }
            )
        }
        val normalized = request.copy(
            play_events = request.play_events.map { it.copy(track = it.track.canonicalForLan()) },
            playback_session = request.playback_session?.let { snapshot ->
                snapshot.copy(
                    current_track = snapshot.current_track?.canonicalForLan(),
                    queue = snapshot.queue.map(Track::canonicalForLan),
                )
            },
        )
        val fields = wire.encodeToJsonElement(normalized).jsonObject.toMutableMap()
        // A feedback receipt id is distinct from the song id checked by the desktop.
        require(
            request.feedback.all {
                it.action in setOf("replay", "like_explicit", "not_interested", "relisten_later")
            }
        )
        if (request.feedback.isNotEmpty())
            fields["feedback"] =
                JsonArray(
                    request.feedback.map {
                        // Desktop stores client_event_id but accepts the idempotency key as
                        // event_id.
                        val provider = lanProviderFromKey(it.track_key)
                        val identity = it.track_key.removePrefix("$provider:")
                        require(identity.isNotEmpty() && identity != it.track_key && it.canSync())
                        buildJsonObject {
                            put("client_event_id", requireNotNull(it.client_event_id))
                            put("event_id", it.client_event_id)
                            put("provider", provider)
                            put("external_id", identity)
                            put("track_key", it.track_key)
                            put("action", it.action)
                            put("context", it.context)
                            if (provider == "netease") {
                                val id = identity.toLong()
                                require(id > 0 && (it.song_id == 0L || it.song_id == id))
                                put("song_id", id)
                            }
                        }
                    }
                )
        return JsonObject(fields)
    }

    internal fun acceptsAccount(account: String?) =
        account == null || config.accountId.isBlank() || account == config.accountId

    suspend fun fetchHandoff() =
        wire.decodeFromJsonElement<HandoffSnapshot?>(request("handoff"))?.also {
            require(acceptsAccount(it.account_id))
        }

    suspend fun pushHandoff(snapshot: HandoffSnapshot): HandoffSnapshot {
        require(
            requireNotNull(snapshot.track).provider != "device" &&
                snapshot.queue.none { it.provider == "device" }
        )
        val body =
            JsonObject(
                wire.encodeToJsonElement(
                    snapshot.copy(
                        track = snapshot.track?.canonicalForLan(),
                        queue = snapshot.queue.map(Track::canonicalForLan),
                    )
                ).jsonObject +
                    ("account_id" to JsonPrimitive(config.accountId))
            )
        return wire.decodeFromJsonElement(request("handoff", body))
    }

    suspend fun ackHandoff(handoffId: String) =
        request(
            "handoff/ack",
            buildJsonObject {
                put("handoff_id", handoffId)
                put("account_id", config.accountId)
            },
        )

    suspend fun fetchLocalLibrary(query: String = "", limit: Int = 200, offset: Int = 0) =
        wire.decodeFromJsonElement<LocalPage>(
            request(
                "local-library",
                query =
                    mapOf("q" to query, "limit" to limit.toString(), "offset" to offset.toString()),
            )
        )

    suspend fun searchMulti(
        query: String,
        limit: Int = 30,
        offset: Int = 0,
        channels: String = "",
    ): LanMultiSearchResponse {
        val params = buildMap {
            put("q", query)
            put("limit", limit.coerceIn(1, 100).toString())
            put("offset", offset.coerceAtLeast(0).toString())
            if (channels.isNotBlank()) put("channels", channels.trim())
        }
        return wire.decodeFromJsonElement(request("search/multi", query = params))
    }

    suspend fun resolveProxyAudio(
        track: Track,
        quality: AudioQualityLevel = AudioQualityLevel.jymaster,
    ): JsonObject =
        request(
            "audio/resolve",
            buildJsonObject {
                put("track", wire.encodeToJsonElement(track.canonicalForLan()))
                put("quality", quality.name)
            },
        ).jsonObject

    suspend fun fetchProxyLyrics(track: Track): JsonObject {
        val normalized = track.canonicalForLan()
        val params = buildMap<String, String> {
            (normalized.song_id ?: normalized.id)?.takeIf { it > 0L }?.let { put("id", it.toString()) }
            normalized.track_key?.takeIf(String::isNotBlank)?.let { put("track_key", it) }
            normalized.effectiveProvider().takeIf(String::isNotBlank)?.let { put("provider", it) }
            normalized.external_id?.takeIf(String::isNotBlank)?.let { put("external_id", it) }
            normalized.title.takeIf(String::isNotBlank)?.let { put("title", it) }
            val artist = normalized.artist.ifBlank { normalized.artists.joinToString(" / ") }
            if (artist.isNotBlank()) put("artist", artist)
        }
        return request("lyrics", query = params).jsonObject
    }

    internal fun revoke() {
        close()
        notifyRevoked()
    }

    private fun notifyRevoked() {
        if (revocationNotified.compareAndSet(false, true)) onRevoke(this)
    }
}
