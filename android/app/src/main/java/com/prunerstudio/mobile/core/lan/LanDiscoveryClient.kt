package com.prunerstudio.mobile.core.lan

import com.prunerstudio.mobile.core.model.wire
import java.net.*
import kotlinx.coroutines.*
import kotlinx.serialization.json.*

data class Announcement(
    val host: String,
    val port: Int,
    val name: String,
    val fingerprint: String,
    val pairingActive: Boolean,
    val spkiPin: String? = null,
)

class LanDiscoveryClient {
    suspend fun discover(timeoutMs: Int = 1500): List<Announcement> =
        withContext(Dispatchers.IO) {
            require(timeoutMs in 1..60000)
            DatagramSocket().use { socket ->
                socket.broadcast = true
                val request = "PRUNER_DISCOVER_V1".toByteArray()
                socket.send(
                    DatagramPacket(
                        request,
                        request.size,
                        InetAddress.getByName("255.255.255.255"),
                        38765,
                    )
                )
                val deadline = System.nanoTime() + timeoutMs * 1000000L
                val found = mutableSetOf<Announcement>()
                while (isActive) {
                    val left = (deadline - System.nanoTime()) / 1000000
                    if (left <= 0) break
                    socket.soTimeout = left.toInt()
                    val packet = DatagramPacket(ByteArray(4096), 4096)
                    try {
                        socket.receive(packet)
                    } catch (_: SocketTimeoutException) {
                        break
                    }
                    runCatching {
                        val obj =
                            wire
                                .parseToJsonElement(
                                    String(packet.data, 0, packet.length, Charsets.UTF_8)
                                )
                                .jsonObject
                        fun field(key: String) = obj.getValue(key).jsonPrimitive
                        require(
                            field("type").content == "pruner_announce" &&
                                field("v").int == 1 &&
                                field("tls").boolean
                        )
                        val port = field("port").int
                        val pin = field("cert_fingerprint").content
                        require(port in 1024..65535 && PinnedTlsFactory.isValidPin(pin))
                        val spkiRaw =
                            obj["spki_pin"]?.jsonPrimitive?.contentOrNull
                                ?: obj["spki_fingerprint"]?.jsonPrimitive?.contentOrNull
                        val spki =
                            spkiRaw?.let {
                                runCatching { PinnedTlsFactory.normalizeSpkiPin(it) }.getOrNull()
                            }
                        found +=
                            Announcement(
                                packet.address.hostAddress!!,
                                port,
                                field("device_name").content,
                                pin,
                                field("pairing_active").boolean,
                                spkiPin = spki,
                            )
                    }
                }
                found.toList()
            }
        }
}
