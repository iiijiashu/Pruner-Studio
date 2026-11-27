package com.prunerstudio.mobile.core.lan

import com.prunerstudio.mobile.core.model.*
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.*
import kotlinx.serialization.json.*
import okhttp3.*
import okhttp3.sse.*

sealed interface LanSseEvent {
    data object Connected : LanSseEvent

    data object DeviceRevoked : LanSseEvent

    data class Handoff(val snapshot: HandoffSnapshot) : LanSseEvent

    data class SyncAvailable(val accountId: String?, val serverTime: String?) : LanSseEvent

    data class Error(val message: String) : LanSseEvent
}

class LanSseClient(private val lan: LanSyncClient) {
    fun events(): Flow<LanSseEvent> = callbackFlow {
        val client = lan.client.newBuilder().readTimeout(0, TimeUnit.MILLISECONDS).build()
        val listener =
            object : EventSourceListener() {
                override fun onEvent(
                    eventSource: EventSource,
                    id: String?,
                    type: String?,
                    data: String,
                ) {
                    val event =
                        runCatching {
                                when (type) {
                                    "connected" -> LanSseEvent.Connected
                                    "handoff" -> {
                                        val snapshot = wire.decodeFromString<HandoffSnapshot>(data)
                                        if (!lan.acceptsAccount(snapshot.account_id)) return
                                        LanSseEvent.Handoff(snapshot)
                                    }
                                    "sync_available" -> {
                                        val fields = wire.parseToJsonElement(data).jsonObject
                                        val account =
                                            fields["account_id"]
                                                ?.jsonPrimitive
                                                ?.contentOrNull
                                                ?.takeUnless(String::isBlank)
                                        if (!lan.acceptsAccount(account)) return
                                        LanSseEvent.SyncAvailable(
                                            account,
                                            fields["server_time"]?.jsonPrimitive?.contentOrNull,
                                        )
                                    }
                                    "device_revoked" -> {
                                        lan.revoke()
                                        LanSseEvent.DeviceRevoked
                                    }
                                    else -> return
                                }
                            }
                            .getOrElse { LanSseEvent.Error("Malformed event") }
                    if (trySend(event).isFailure || event == LanSseEvent.DeviceRevoked) close()
                }

                override fun onFailure(
                    eventSource: EventSource,
                    t: Throwable?,
                    response: Response?,
                ) {
                    trySend(
                        if (response?.code == 401) LanSseEvent.DeviceRevoked
                        else LanSseEvent.Error(t?.message ?: "HTTP ${response?.code}")
                    )
                    close()
                }

                override fun onClosed(eventSource: EventSource) {
                    close()
                }
            }
        val source =
            EventSources.createFactory(client)
                .newEventSource(
                    Request.Builder().url(lan.base.resolve("events")!!).build(),
                    listener,
                )
        awaitClose { source.cancel() }
    }
}
