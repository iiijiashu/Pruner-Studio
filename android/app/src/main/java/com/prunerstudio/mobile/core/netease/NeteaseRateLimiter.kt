package com.prunerstudio.mobile.core.netease

import com.prunerstudio.mobile.core.model.AudioErrorCategory
import com.prunerstudio.mobile.core.model.AudioResolutionException
import kotlin.math.min
import kotlin.random.Random
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

private val rateClockStartNanos = System.nanoTime()

data class WriteRateConfig(
    val minIntervalMs: Long = 850L,
    val jitterMaxMs: Long = 350L,
    val microBatchSize: Int = 5,
    val microBatchPauseMs: Long = 1_800L,
    val baseCooldownMs: Long = 30_000L,
    val maxCooldownMs: Long = 300_000L,
    val readRetryMaxAttempts: Int = 2,
    val readRetryBaseMs: Long = 1_000L,
    val readRetryCapMs: Long = 5_000L,
)

data class BatchWriteResult(
    val succeededIds: List<Long>,
    val failedIds: List<Long>,
    val circuitTripped: Boolean = false,
    val errorMessage: String? = null,
)

/**
 * Serializes NetEase Cloud Music write operations with pacing + jitter, micro-batch pauses,
 * and an exponential-backoff circuit breaker for HTTP 429 and NetEase risk-control codes
 * (-460, -462, -447).
 */
class NeteaseRateLimiter(
    val config: WriteRateConfig = WriteRateConfig(),
    private val nowMs: () -> Long = { (System.nanoTime() - rateClockStartNanos) / 1_000_000L + 1 },
    private val sleeper: suspend (Long) -> Unit = { delay(it) },
    private val jitterProvider: (Long) -> Long = { max ->
        if (max <= 0L) 0L else Random.nextLong(0L, max + 1L)
    },
) {
    private val writeMutex = Mutex()
    private val stateLock = Any()

    @Volatile private var lastWriteAtMs: Long = 0L
    @Volatile private var circuitOpenUntilMs: Long = 0L
    @Volatile private var consecutiveStrikes: Int = 0
    @Volatile private var lastTriggerCode: Int? = null

    fun isRiskControlOrRateLimit(httpCode: Int, businessCode: Int?): Boolean =
        httpCode == 429 || businessCode in RISK_CONTROL_CODES

    fun isCircuitOpen(): Boolean = remainingCooldownMs() > 0L

    fun remainingCooldownMs(): Long {
        val remaining = circuitOpenUntilMs - nowMs()
        return if (remaining > 0L) remaining else 0L
    }

    fun checkCircuit() {
        val remaining = remainingCooldownMs()
        if (remaining > 0L) {
            val sec = ((remaining + 999L) / 1000L).coerceAtLeast(1L)
            val codeHint = lastTriggerCode?.let { " ($it)" }.orEmpty()
            throw AudioResolutionException(
                AudioErrorCategory.RATE_LIMITED,
                "触发网易云风控或限流$codeHint，写入已自动熔断保护，请 ${sec} 秒后重试",
            )
        }
    }

    suspend fun acquireWriteSlot() {
        writeMutex.withLock {
            checkCircuit()
            val now = nowMs()
            val waitMs =
                if (lastWriteAtMs == 0L) 0L
                else {
                    val target =
                        lastWriteAtMs + config.minIntervalMs + jitterProvider(config.jitterMaxMs)
                    (target - now).coerceAtLeast(0L)
                }
            if (waitMs > 0L) sleeper(waitMs)
            checkCircuit()
            lastWriteAtMs = nowMs()
        }
    }

    suspend fun pauseBetweenMicroBatches() {
        checkCircuit()
        val pause = config.microBatchPauseMs + jitterProvider(config.jitterMaxMs)
        if (pause > 0L) sleeper(pause)
        checkCircuit()
    }

    suspend fun backoffForReadRetry(attempt: Int): Boolean {
        if (attempt >= config.readRetryMaxAttempts) return false
        val exponential = config.readRetryBaseMs * (1L shl attempt.coerceAtMost(6))
        val waitMs =
            maxOf(exponential, remainingCooldownMs()) + jitterProvider(config.jitterMaxMs)
        if (waitMs > config.readRetryCapMs) return false
        if (waitMs > 0L) sleeper(waitMs)
        return !isCircuitOpen()
    }

    fun recordRateLimit(
        httpCode: Int,
        businessCode: Int?,
        retryAfterSec: Long? = null,
        read: Boolean = false,
    ): Long {
        synchronized(stateLock) {
            consecutiveStrikes = (consecutiveStrikes + 1).coerceAtMost(6)
            lastTriggerCode = businessCode ?: httpCode
            val expCooldown =
                config.baseCooldownMs * (1L shl (consecutiveStrikes - 1).coerceAtMost(5))
            val now = nowMs()
            val retryAfterMs = retryAfterSec?.coerceIn(0L, Long.MAX_VALUE / 1000L)?.times(1000L)
            val cooldown = if (read && retryAfterMs != null) {
                retryAfterMs.coerceAtLeast(config.readRetryBaseMs)
            } else {
                maxOf(min(config.maxCooldownMs, expCooldown), retryAfterMs ?: 0L)
            }
            circuitOpenUntilMs = maxOf(circuitOpenUntilMs, now + cooldown.coerceAtMost(Long.MAX_VALUE - now))
            return cooldown
        }
    }

    fun recordSuccess() {
        synchronized(stateLock) {
            if (consecutiveStrikes > 0 && nowMs() >= circuitOpenUntilMs) {
                consecutiveStrikes = 0
                lastTriggerCode = null
            }
        }
    }

    fun reset() {
        synchronized(stateLock) {
            consecutiveStrikes = 0
            circuitOpenUntilMs = 0L
            lastWriteAtMs = 0L
            lastTriggerCode = null
        }
    }

    companion object {
        val RISK_CONTROL_CODES = setOf(-460, -462, -447, 429)
    }
}
