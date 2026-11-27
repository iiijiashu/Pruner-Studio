package com.prunerstudio.mobile.core.netease

import java.io.IOException
import kotlinx.coroutines.*
import kotlinx.coroutines.selects.select
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

class AuthWriteSession internal constructor(
    val accountId: String,
    internal val cookie: String,
    val generation: Long,
    private val invalidated: Deferred<Unit>,
) {
    internal fun requireCurrent() {
        if (invalidated.isCompleted) throw IOException("NetEase login session changed")
    }

    internal suspend fun awaitPacing(wait: suspend () -> Unit) = coroutineScope {
        requireCurrent()
        val pacing = async(start = CoroutineStart.UNDISPATCHED) { wait() }
        select<Unit> {
            invalidated.onAwait {
                pacing.cancel()
                requireCurrent()
            }
            pacing.onAwait { requireCurrent() }
        }
    }

    internal fun onInvalidation(cancel: () -> Unit): DisposableHandle =
        invalidated.invokeOnCompletion { cancel() }
}

class AuthSessionCoordinator(private val credentials: SecureCredentialStore) {
    private val mutex = Mutex()
    private var generation = 0L
    private var writeGeneration = 0L
    private var writeEpoch = CompletableDeferred<Unit>()
    private var poll: Job? = null

    suspend fun captureWriteSession(accountId: String): AuthWriteSession? = mutex.withLock {
        val cookie = withContext(Dispatchers.IO) { credentials.cookie } ?: return@withLock null
        AuthWriteSession(accountId, cookie, writeGeneration, writeEpoch)
    }

    private fun advanceWriteGeneration() {
        writeGeneration++
        writeEpoch.complete(Unit)
        writeEpoch = CompletableDeferred()
    }

    suspend fun invalidateWrites() = mutex.withLock { advanceWriteGeneration() }

    suspend fun start(scope: CoroutineScope, block: suspend (Long) -> Unit): Job = mutex.withLock {
        poll?.cancel()
        advanceWriteGeneration()
        val token = ++generation
        scope.launch(start = CoroutineStart.LAZY) { block(token) }.also {
            poll = it
            it.start()
        }
    }

    suspend fun cancel(logout: Boolean = false) = mutex.withLock {
        generation++
        advanceWriteGeneration()
        poll?.cancel()
        poll = null
        if (logout) withContext(Dispatchers.IO) { credentials.cookie = null }
    }

    suspend fun commit(token: Long, cookie: String): Boolean = mutex.withLock {
        if (token != generation || poll?.isActive != true) return false
        advanceWriteGeneration()
        withContext(Dispatchers.IO) { credentials.cookie = cookie }
        true
    }
}
