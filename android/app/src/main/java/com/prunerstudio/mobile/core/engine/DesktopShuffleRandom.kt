package com.prunerstudio.mobile.core.engine

import java.math.BigInteger

/**
 * CPython integer-seeded MT19937. Desktop shuffle uses random() for coordinates and getrandbits()
 * with rejection sampling for Fisher–Yates indices.
 */
internal class DesktopShuffleRandom(seed: Long) {
    private val state = IntArray(624)
    private var cursor = state.size

    init {
        val magnitude = BigInteger.valueOf(seed).abs()
        val key =
            IntArray(maxOf(1, (magnitude.bitLength() + 31) / 32)) {
                magnitude.shiftRight(it * 32).toInt()
            }
        state[0] = 19650218
        for (i in 1 until state.size) state[i] = 1812433253 * mix(state[i - 1]) + i
        var i = 1
        var j = 0
        repeat(maxOf(state.size, key.size)) {
            state[i] = (state[i] xor (mix(state[i - 1]) * 1664525)) + key[j] + j
            i++
            j = (j + 1) % key.size
            if (i == state.size) {
                state[0] = state.last()
                i = 1
            }
        }
        repeat(state.size - 1) {
            state[i] = (state[i] xor (mix(state[i - 1]) * 1566083941)) - i
            i++
            if (i == state.size) {
                state[0] = state.last()
                i = 1
            }
        }
        state[0] = Int.MIN_VALUE
    }

    private fun mix(value: Int) = value xor (value ushr 30)

    private fun nextWord(): Int {
        if (cursor == state.size) {
            for (i in state.indices) {
                val bits = (state[i] and Int.MIN_VALUE) or (state[(i + 1) % 624] and Int.MAX_VALUE)
                state[i] =
                    state[(i + 397) % 624] xor
                        (bits ushr 1) xor
                        (if (bits and 1 == 1) 0x9908b0df.toInt() else 0)
            }
            cursor = 0
        }
        var x = state[cursor++]
        x = x xor (x ushr 11)
        x = x xor ((x shl 7) and 0x9d2c5680.toInt())
        x = x xor ((x shl 15) and 0xefc60000.toInt())
        return x xor (x ushr 18)
    }

    fun fraction() = ((nextWord() ushr 5) * 67108864.0 + (nextWord() ushr 6)) / 9007199254740992.0

    fun uniform(lo: Double, hi: Double) = lo + (hi - lo) * fraction()

    fun <T> shuffle(input: List<T>): List<T> {
        val items = input.toMutableList()
        for (i in items.lastIndex downTo 1) {
            val n = i + 1
            var j: Int
            do {
                j = nextWord() ushr Integer.numberOfLeadingZeros(n)
            } while (j >= n)
            java.util.Collections.swap(items, i, j)
        }
        return items
    }
}
