package com.prunerstudio.mobile.core.lan

import com.prunerstudio.mobile.core.db.PrunerRepository
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.SecureCredentialStore
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

data class LanSessionState(
    val initialized: Boolean = false,
    val pairing: PairedServerConfig? = null,
    val sseConnected: Boolean = false,
    val syncing: Boolean = false,
    val pendingHandoff: HandoffSnapshot? = null,
    val lastSyncAt: Long? = null,
    val lastError: String? = null,
    val lanBrowseTracks: List<LanLocalFile> = emptyList(),
    val lanBrowseQuery: String = "",
    val lanBrowseHasMore: Boolean = false,
    val searchResults: List<LanLocalFile> = emptyList(),
    val pairingClaimInFlight: Boolean = false,
)

// Session ownership and UI publication are confined to Main; SQLite shares the transition mutex.
class LanSessionCoordinator(
    private val credentials: SecureCredentialStore,
    private val repository: PrunerRepository,
    private val resolveClient: () -> LanSyncClient?,
    private val installPairing: (PairedServerConfig?) -> LanSyncClient?,
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val mutex = Mutex()
    private val mutable = MutableStateFlow(LanSessionState())
    val state = mutable.asStateFlow()
    private var pairingSessionJob = SupervisorJob()
    @Volatile private var pairingGeneration = 0L
    private var client: LanSyncClient? = null
    private var sseJob: Job? = null
    private var browseJob: Job? = null
    private var searchJob: Job? = null
    private var syncJob: Job? = null
    private var claimJob: Job? = null

    suspend fun ensureConnected() = mutex.withLock {
        if (!state.value.initialized) {
            client = withContext(Dispatchers.IO) { resolveClient() }
            mutable.value = LanSessionState(initialized = true, pairing = client?.config)
        }
        connect()
    }

    private inner class Session(val generation: Long, val lan: LanSyncClient) {
        fun active() =
            !lan.isClosed && pairingGeneration == generation && state.value.pairing == lan.config

        fun publish(change: (LanSessionState) -> LanSessionState) {
            if (active()) mutable.update(change)
        }

        fun offer(snapshot: HandoffSnapshot?) = publish { current ->
            current.copy(pendingHandoff = snapshot?.takeIf {
                it.track != null && it.source_device_id != lan.config.deviceId &&
                    lan.config.deviceId !in it.consumed_by
            })
        }
    }

    private fun work(block: suspend Session.() -> Unit): Job? {
        val session = Session(pairingGeneration, client ?: return null)
        return scope.launch(pairingSessionJob) {
            try {
                session.block()
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                val message =
                    when (e) {
                        is javax.net.ssl.SSLHandshakeException -> "桌面证书指纹不匹配，请重新配对"
                        is java.net.ConnectException,
                        is java.net.SocketTimeoutException,
                        is java.net.UnknownHostException -> "无法连接桌面，请确认桌面已开启且在同一局域网"
                        else -> "桌面连接失败"
                    }
                session.publish { it.copy(lastError = message) }
            }
        }
    }

    private fun connect() {
        if (sseJob?.isActive == true) return
        sseJob = work {
            while (active()) {
                LanSseClient(lan).events().flowOn(Dispatchers.IO).collect { event ->
                    if (!active()) return@collect
                    when (event) {
                        LanSseEvent.Connected -> {
                            publish { it.copy(sseConnected = true, lastError = null) }
                            pull()
                        }
                        LanSseEvent.DeviceRevoked -> revoke(lan)
                        is LanSseEvent.SyncAvailable -> sync()
                        is LanSseEvent.Handoff -> offer(event.snapshot)
                        is LanSseEvent.Error -> publish { it.copy(lastError = "连接中断") }
                    }
                }
                publish { it.copy(sseConnected = false) }
                delay(5000)
            }
        }
    }

    fun revoked(expected: LanSyncClient) {
        // Post the teardown so a caller on Main can handle the denial first.
        scope.launch(Dispatchers.Main) { revoke(expected) }
    }

    private suspend fun revoke(expected: LanSyncClient) = mutex.withLock {
        val current = if (state.value.initialized) client else withContext(Dispatchers.IO) { resolveClient() }
        if (current === expected) replace(null)
    }

    suspend fun unpair() = mutex.withLock { replace(null) }

    // Called with mutex held. Finish persistence and publication together after admission.
    private suspend fun replace(next: PairedServerConfig?) = withContext(NonCancellable) {
        pairingGeneration++
        pairingSessionJob.cancel()
        pairingSessionJob = SupervisorJob()
        sseJob = null
        claimJob?.cancel()
        claimJob = null
        val previous = client
        client = null
        mutable.value = LanSessionState(initialized = false)
        client = withContext(Dispatchers.IO) {
            previous?.close()
            installPairing(next)
        }
        mutable.value = LanSessionState(initialized = true, pairing = client?.config)
        connect()
    }

    suspend fun claim(candidate: PairingUri, expected: PairedServerConfig?): Boolean {
        val owner = currentCoroutineContext().job
        val generation = mutex.withLock {
            if (!state.value.initialized || state.value.pairing != expected || claimJob != null) return false
            claimJob = owner
            mutable.update { it.copy(pairingClaimInFlight = true) }
            pairingGeneration
        }
        try {
            val next = LanSyncClient.forPairing(candidate, credentials)
                .claimPairing(candidate.code, android.os.Build.MODEL)
            return mutex.withLock {
                owner.ensureActive()
                if (pairingGeneration != generation || claimJob !== owner) return false
                claimJob = null
                replace(next)
                sync()
                true
            }
        } finally {
            withContext(NonCancellable) {
                mutex.withLock {
                    if (claimJob === owner) {
                        claimJob = null
                        mutable.update { it.copy(pairingClaimInFlight = false) }
                    }
                }
            }
        }
    }

    fun sync() {
        if (syncJob?.isActive == true) return
        syncJob = work {
            publish { it.copy(syncing = true) }
            try {
                repository.syncWithDesktop(lan, sessionMutex = mutex, isStillActive = ::active)
                publish { it.copy(lastSyncAt = System.currentTimeMillis(), lastError = null) }
            } finally {
                publish { it.copy(syncing = false) }
            }
        }
    }

    fun browse(query: String, append: Boolean) {
        browseJob?.cancel()
        browseJob = work {
            val previous = state.value.lanBrowseTracks.takeIf { append && state.value.lanBrowseQuery == query }.orEmpty()
            publish { it.copy(lanBrowseQuery = query, lanBrowseTracks = previous, lanBrowseHasMore = false) }
            val page = lan.fetchLocalLibrary(query, 50, previous.size)
            publish { it.copy(lanBrowseTracks = previous + page.files, lanBrowseHasMore = page.has_more) }
        }
    }

    fun search(query: String) {
        searchJob?.cancel()
        searchJob = work {
            publish { it.copy(searchResults = emptyList()) }
            if (query.isBlank()) return@work
            delay(250)
            val page = lan.fetchLocalLibrary(query, 100)
            publish { it.copy(searchResults = page.files) }
        }
    }

    fun pull() { work { offer(lan.fetchHandoff()) } }
    fun dismissHandoff() { work { offer(null) } }
    fun push(snapshot: HandoffSnapshot, sent: () -> Unit) {
        work {
            lan.pushHandoff(snapshot)
            if (active()) sent()
        }
    }

    fun accept(snapshot: HandoffSnapshot, play: suspend (String) -> Unit) {
        work {
            if (state.value.pendingHandoff != snapshot || !lan.acceptsAccount(snapshot.account_id)) return@work
            play(lan.config.accountId)
            snapshot.handoff_id?.let { lan.ackHandoff(it) }
            if (state.value.pendingHandoff == snapshot) offer(null)
        }
    }
}
