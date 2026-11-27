package com.prunerstudio.mobile.core.player

import android.content.Context
import android.media.audiofx.DynamicsProcessing
import android.media.audiofx.Equalizer
import android.os.Build
import androidx.annotation.RequiresApi
import androidx.core.content.edit
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow

data class EqState(
    val enabled: Boolean = false,
    val engineName: String = "DynamicsProcessing 10段",
    val bandFrequenciesHz: List<Int> = ISO_10_BANDS_HZ,
    val bandGainsDb: List<Float> = List(ISO_10_BANDS_HZ.size) { 0f },
    val minDb: Float = -12f,
    val maxDb: Float = 12f,
    val presetName: String = "平直",
    val hardwareAvailable: Boolean = true,
) {
    companion object {
        val ISO_10_BANDS_HZ = listOf(31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)

        val PRESETS: Map<String, List<Float>> =
            linkedMapOf(
                "平直" to listOf(0f, 0f, 0f, 0f, 0f, 0f, 0f, 0f, 0f, 0f),
                "人声清晰" to listOf(-2f, -1f, 0f, 1.5f, 3f, 3.5f, 2.5f, 1f, 0f, -1f),
                "低音增强" to listOf(5f, 4.5f, 3f, 1.5f, 0f, 0f, 0f, 0.5f, 1f, 1f),
                "温暖黑胶" to listOf(2f, 2.5f, 1.5f, 1f, 0.5f, -0.5f, -1f, -1.5f, -2f, -2.5f),
                "古典现场" to listOf(2f, 1.5f, 0.5f, 0f, 0f, 0.5f, 1.5f, 2.5f, 3f, 2f),
                "流行节奏" to listOf(2.5f, 2f, 0.5f, -0.5f, 0.5f, 1.5f, 2f, 2f, 1.5f, 1f),
            )
    }
}

/**
 * Honest hardware-backed audio equalizer:
 * - API 28+: uses real 10-band ISO `DynamicsProcessing` EQ (-12dB..+12dB).
 * - API 26-27 or fallback: uses real hardware `Equalizer` reporting actual band count and center frequencies.
 * Persists enabled state, preset, and per-band dB values in SharedPreferences and re-applies on audio session changes.
 */
class EqualizerController(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("pruner_ui_prefs", 0)
    private var dynamics: DynamicsProcessing? = null
    private var legacyEq: Equalizer? = null

    private val _state = MutableStateFlow(loadPersistedState())
    val state = _state.asStateFlow()

    private fun loadPersistedState(): EqState {
        val enabled = prefs.getBoolean(KEY_ENABLED, false)
        val preset = prefs.getString(KEY_PRESET, "平直").orEmpty().ifBlank { "平直" }
        val savedGains =
            prefs
                .getString(KEY_GAINS, null)
                ?.split(',')
                ?.mapNotNull { it.toFloatOrNull() }
                ?.takeIf { it.isNotEmpty() }
                ?: List(EqState.ISO_10_BANDS_HZ.size) { 0f }
        val gains =
            List(EqState.ISO_10_BANDS_HZ.size) { i ->
                savedGains.getOrElse(i) { 0f }.coerceIn(-12f, 12f)
            }
        return EqState(
            enabled = enabled,
            bandFrequenciesHz = EqState.ISO_10_BANDS_HZ,
            bandGainsDb = gains,
            presetName = preset,
        )
    }

    private fun persist(state: EqState) {
        prefs.edit {
            putBoolean(KEY_ENABLED, state.enabled)
            putString(KEY_PRESET, state.presetName)
            putString(
                KEY_GAINS,
                state.bandGainsDb.joinToString(",") { "%.2f".format(java.util.Locale.ROOT, it) },
            )
        }
    }

    fun attachToSession(audioSessionId: Int) {
        releaseEffects()
        if (audioSessionId <= 0) return

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            val attachedDynamics =
                runCatching {
                    val bands = EqState.ISO_10_BANDS_HZ
                    val config = buildDynamicsConfig(bands.size)
                    DynamicsProcessing(0, audioSessionId, config).also { dp ->
                        val eq = DynamicsProcessing.Eq(true, true, bands.size)
                        bands.forEachIndexed { idx, freqHz ->
                            val cutoff =
                                if (idx == bands.lastIndex) 20000f
                                else (freqHz * 1.414f).coerceAtMost(20000f)
                            val band =
                                DynamicsProcessing.EqBand(
                                    true,
                                    cutoff,
                                    _state.value.bandGainsDb.getOrElse(idx) { 0f },
                                )
                            eq.setBand(idx, band)
                        }
                        dp.setPostEqAllChannelsTo(eq)
                        dp.enabled = _state.value.enabled
                    }
                }
                    .getOrNull()
            if (attachedDynamics != null) {
                dynamics = attachedDynamics
                _state.value =
                    _state.value.copy(
                        engineName = "DynamicsProcessing 10段",
                        bandFrequenciesHz = EqState.ISO_10_BANDS_HZ,
                        minDb = -12f,
                        maxDb = 12f,
                        hardwareAvailable = true,
                    )
                return
            }
        }

        val attachedLegacy =
            runCatching {
                Equalizer(0, audioSessionId).also { eq ->
                    val count = eq.numberOfBands.toInt().coerceAtLeast(1)
                    val range = eq.bandLevelRange
                    val minDb = (range[0] / 100f).coerceAtLeast(-15f)
                    val maxDb = (range[1] / 100f).coerceAtMost(15f)
                    val freqs = (0 until count).map { (eq.getCenterFreq(it.toShort()) / 1000).coerceAtLeast(20) }
                    val prev = _state.value.bandGainsDb
                    val gains =
                        (0 until count).map { i ->
                            prev.getOrElse(i) { 0f }.coerceIn(minDb, maxDb)
                        }
                    gains.forEachIndexed { i, db ->
                        eq.setBandLevel(i.toShort(), (db * 100f).toInt().toShort())
                    }
                    eq.enabled = _state.value.enabled
                    _state.value =
                        _state.value.copy(
                            engineName = "硬件均衡器 ${count}段",
                            bandFrequenciesHz = freqs,
                            bandGainsDb = gains,
                            minDb = minDb,
                            maxDb = maxDb,
                            hardwareAvailable = true,
                        )
                }
            }
                .getOrNull()
        if (attachedLegacy != null) {
            legacyEq = attachedLegacy
        } else {
            _state.value = _state.value.copy(hardwareAvailable = false)
        }
    }

    fun setEnabled(enabled: Boolean): Boolean {
        val next = _state.value.copy(enabled = enabled)
        _state.value = next
        persist(next)
        return applyToHardware(next)
    }

    fun setBandDb(bandIndex: Int, gainDb: Float): Boolean {
        val cur = _state.value
        if (bandIndex !in cur.bandGainsDb.indices) return false
        val clamped = gainDb.coerceIn(cur.minDb, cur.maxDb)
        val updated = cur.bandGainsDb.toMutableList().apply { set(bandIndex, clamped) }
        val next = cur.copy(enabled = true, bandGainsDb = updated, presetName = "自定义")
        _state.value = next
        persist(next)
        return applyToHardware(next)
    }

    /** Legacy normalized [-1f..1f] compatibility adapter. */
    fun setNormalizedGain(bandIndex: Int, normalized: Float): Boolean {
        val cur = _state.value
        val db =
            if (normalized >= 0f) normalized.coerceAtMost(1f) * cur.maxDb
            else (-normalized.coerceAtLeast(-1f)) * cur.minDb
        return setBandDb(bandIndex, db)
    }

    fun applyPreset(name: String): Boolean {
        val template = EqState.PRESETS[name] ?: return false
        val cur = _state.value
        val bandCount = cur.bandFrequenciesHz.size
        val mapped =
            (0 until bandCount).map { idx ->
                val mappedIdx =
                    if (bandCount == template.size) idx
                    else ((idx.toFloat() / (bandCount - 1).coerceAtLeast(1)) * (template.size - 1)).toInt()
                template[mappedIdx.coerceIn(template.indices)].coerceIn(cur.minDb, cur.maxDb)
            }
        val enable = name != "平直" || cur.enabled
        val next = cur.copy(enabled = enable, bandGainsDb = mapped, presetName = name)
        _state.value = next
        persist(next)
        return applyToHardware(next)
    }

    private fun applyToHardware(state: EqState): Boolean {
        val dp = dynamics
        if (dp != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            return runCatching {
                val eq = DynamicsProcessing.Eq(true, state.enabled, state.bandGainsDb.size)
                state.bandFrequenciesHz.forEachIndexed { idx, freqHz ->
                    val cutoff =
                        if (idx == state.bandFrequenciesHz.lastIndex) 20000f
                        else (freqHz * 1.414f).coerceAtMost(20000f)
                    eq.setBand(
                        idx,
                        DynamicsProcessing.EqBand(true, cutoff, state.bandGainsDb.getOrElse(idx) { 0f }),
                    )
                }
                dp.setPostEqAllChannelsTo(eq)
                dp.enabled = state.enabled
            }
                .isSuccess
        }
        val eq = legacyEq
        if (eq != null) {
            return runCatching {
                val count = eq.numberOfBands.toInt()
                for (i in 0 until count) {
                    val db = state.bandGainsDb.getOrElse(i) { 0f }
                    eq.setBandLevel(i.toShort(), (db * 100f).toInt().toShort())
                }
                eq.enabled = state.enabled
            }
                .isSuccess
        }
        // State is persisted even before playback session starts; will auto-apply on attachToSession.
        return true
    }

    fun releaseEffects() {
        runCatching { dynamics?.release() }
        dynamics = null
        runCatching { legacyEq?.release() }
        legacyEq = null
    }

    companion object {
        private const val KEY_ENABLED = "eq_enabled"
        private const val KEY_PRESET = "eq_preset"
        private const val KEY_GAINS = "eq_gains_db"

        /**
         * Builds a stereo 10-band Post-EQ + safety limiter [DynamicsProcessing.Config].
         * Argument order: (variant, channelCount, preEqInUse, preEqBandCount, mbcInUse,
         * mbcBandCount, postEqInUse, postEqBandCount, limiterInUse).
         */
        @RequiresApi(Build.VERSION_CODES.P)
        internal fun buildDynamicsConfig(bandCount: Int = EqState.ISO_10_BANDS_HZ.size): DynamicsProcessing.Config =
            DynamicsProcessing.Config.Builder(
                DynamicsProcessing.VARIANT_FAVOR_FREQUENCY_RESOLUTION,
                2,
                false,
                0,
                false,
                0,
                true,
                bandCount,
                true,
            ).build()
    }
}
