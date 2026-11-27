package com.prunerstudio.mobile.core.engine

/** The portable desktop grammar, with ECMAScript /iu matching and a per-search work limit. */
internal class PortableRegex private constructor(private val expression: Expression) {
    private class Invalid : RuntimeException()

    private class BudgetExceeded : RuntimeException()

    private class Input(text: String) {
        val points = text.codePoints().toArray()
        private var remaining = 100_000

        fun step() {
            if (--remaining < 0) throw BudgetExceeded()
        }
    }

    private data class State(val pos: Int, val captures: Map<Int, IntRange> = emptyMap())

    private fun interface Atom {
        fun match(input: Input, state: State): Sequence<State>
    }

    private class CharacterAtom(val test: (Int) -> Boolean) : Atom {
        override fun match(input: Input, state: State) =
            if (state.pos < input.points.size && test(input.points[state.pos]))
                sequenceOf(state.copy(pos = state.pos + 1))
            else emptySequence()
    }

    private class Piece(
        val atom: Atom,
        val min: Long,
        val max: Long,
        val lazy: Boolean,
        val captures: IntRange,
    ) {
        fun expand(input: Input, state: State, count: Long = 0): Sequence<State> = sequence {
            input.step()
            if (atom is CharacterAtom) {
                var n = 0
                while (
                    n < max &&
                        n < input.points.size - state.pos &&
                        atom.test(input.points[state.pos + n])
                ) {
                    input.step()
                    n++
                }
                if (n >= min) {
                    val lengths = if (lazy) min.toInt()..n else n downTo min.toInt()
                    for (length in lengths) yield(state.copy(pos = state.pos + length))
                }
                return@sequence
            }
            if (lazy && count >= min) yield(state)
            if (count < max) {
                val initial =
                    if (captures.isEmpty() || state.captures.isEmpty()) state
                    else state.copy(captures = state.captures.filterKeys { it !in captures })
                for (next in atom.match(input, initial)) {
                    if (count >= min && next.pos == state.pos) continue
                    if (count + 1 == max) yield(next) else yieldAll(expand(input, next, count + 1))
                }
            }
            if (!lazy && count >= min) yield(state)
        }
    }

    private class Expression(private val alternatives: List<List<Piece>>) : Atom {
        override fun match(input: Input, state: State) =
            alternatives.asSequence().flatMap { branch(input, it, 0, state) }

        private fun branch(
            input: Input,
            terms: List<Piece>,
            index: Int,
            state: State,
        ): Sequence<State> {
            input.step()
            if (index == terms.size) return sequenceOf(state)
            return terms[index].expand(input, state).flatMap { branch(input, terms, index + 1, it) }
        }
    }

    // Returning null lets the caller keep ambiguous/expensive tracks out of pruning results.
    fun matches(text: String): Boolean? =
        try {
            val input = Input(text)
            (0..input.points.size).any { expression.match(input, State(it)).firstOrNull() != null }
        } catch (_: BudgetExceeded) {
            null
        } catch (_: StackOverflowError) {
            null
        }

    private class Parser(private val source: String) {
        private var pos = 0
        private var captures = 0
        private val closed = mutableSetOf<Int>()

        fun parse(): Expression = expression().also { if (pos != source.length) throw Invalid() }

        private fun take(s: String): Boolean {
            if (!source.startsWith(s, pos)) return false
            pos += s.length
            return true
        }

        private fun peek() = source.getOrNull(pos)

        private fun point(): Int {
            if (pos == source.length) throw Invalid()
            return source.codePointAt(pos).also { pos += Character.charCount(it) }
        }

        private fun expression(): Expression {
            val alternatives = mutableListOf<List<Piece>>()
            do {
                val terms = mutableListOf<Piece>()
                while (peek() != null && peek() != ')' && peek() != '|') terms.add(piece())
                alternatives.add(terms)
            } while (take("|"))
            return Expression(alternatives)
        }

        private fun piece(): Piece {
            val firstCapture = captures + 1
            var assertion = false
            val atom =
                when {
                    take("(") -> {
                        val group =
                            when {
                                take("?:") -> ":"
                                take("?=") -> "="
                                take("?!") -> "!"
                                peek() == '?' -> throw Invalid()
                                else -> "capture"
                            }
                        val id = if (group == "capture") ++captures else 0
                        val body = expression()
                        if (!take(")")) throw Invalid()
                        if (id > 0) closed.add(id)
                        assertion = group == "=" || group == "!"
                        Atom { input, state ->
                            val matches = body.match(input, state)
                            when (group) {
                                ":" -> matches
                                "=" ->
                                    matches.firstOrNull()?.let {
                                        sequenceOf(it.copy(pos = state.pos))
                                    } ?: emptySequence()
                                "!" -> if (matches.none()) sequenceOf(state) else emptySequence()
                                else ->
                                    matches.map {
                                        it.copy(
                                            captures =
                                                it.captures + (id to (state.pos until it.pos))
                                        )
                                    }
                            }
                        }
                    }
                    peek() == '^' || peek() == '$' -> {
                        assertion = true
                        val start = take("^")
                        if (!start) take("$")
                        Atom { input, state ->
                            if (state.pos == (if (start) 0 else input.points.size))
                                sequenceOf(state)
                            else emptySequence()
                        }
                    }
                    take("\\b") || take("\\B") -> {
                        assertion = true
                        val boundary = source[pos - 1] == 'b'
                        Atom { input, state ->
                            val left = input.points.getOrNull(state.pos - 1)?.let(::word) == true
                            val right = input.points.getOrNull(state.pos)?.let(::word) == true
                            if ((left != right) == boundary) sequenceOf(state) else emptySequence()
                        }
                    }
                    peek() == '\\' && (source.getOrNull(pos + 1) ?: ' ') in '1'..'9' -> {
                        pos++
                        val start = pos
                        val id = number().toInt()
                        if (pos - start > 2 || id !in closed) throw Invalid()
                        Atom { input, state ->
                            val saved = state.captures[id]
                            if (saved == null) sequenceOf(state)
                            else {
                                var end = state.pos
                                val equal =
                                    saved.all {
                                        input.step()
                                        end < input.points.size &&
                                            fold(input.points[it]) == fold(input.points[end++])
                                    }
                                if (equal) sequenceOf(state.copy(pos = end)) else emptySequence()
                            }
                        }
                    }
                    take("[") -> CharacterAtom(characterClass())
                    take(".") -> CharacterAtom { it !in lineTerminators }
                    peek() in listOf('*', '+', '?', '{', '}', ']') -> throw Invalid()
                    else -> {
                        val test = element(false)
                        CharacterAtom(test.test)
                    }
                }
            val bounds: Pair<Long, Long> =
                when {
                    take("*") -> 0L to Long.MAX_VALUE
                    take("+") -> 1L to Long.MAX_VALUE
                    take("?") -> 0L to 1L
                    take("{") -> {
                        val min = number()
                        val max =
                            if (!take(",")) min else if (peek() == '}') Long.MAX_VALUE else number()
                        if (!take("}") || max < min) throw Invalid()
                        min to max
                    }
                    else -> return Piece(atom, 1, 1, false, firstCapture..captures)
                }
            if (assertion) throw Invalid()
            return Piece(atom, bounds.first, bounds.second, take("?"), firstCapture..captures)
        }

        private fun number(): Long {
            val start = pos
            while ((peek() ?: ' ') in '0'..'9') pos++
            return source.substring(start, pos).toLongOrNull()?.takeIf { it < 4294967295L }
                ?: throw Invalid()
        }

        private data class Element(val literal: Int?, val test: (Int) -> Boolean)

        private fun literal(point: Int) = Element(point) { fold(it) == fold(point) }

        private fun element(inClass: Boolean): Element {
            if (!take("\\")) return literal(point())
            return when (val esc = point().takeIf { it < 128 }?.toChar() ?: throw Invalid()) {
                'd',
                'D' -> Element(null) { (it in 48..57) == (esc == 'd') }
                's',
                'S' -> Element(null) { whitespace(it) == (esc == 's') }
                'w',
                'W' -> Element(null) { word(it) == (esc == 'w') }
                'b' -> if (inClass) literal(8) else throw Invalid()
                't' -> literal(9)
                'n' -> literal(10)
                'v' -> literal(11)
                'f' -> literal(12)
                'r' -> literal(13)
                'x',
                'u' -> {
                    val end = pos + if (esc == 'x') 2 else 4
                    if (end > source.length) throw Invalid()
                    val hex = source.substring(pos, end)
                    if (!hex.all { it in "0123456789abcdefABCDEF" }) throw Invalid()
                    pos = end
                    val cp = hex.toInt(16)
                    if (cp in 0xd800..0xdfff) throw Invalid()
                    literal(cp)
                }
                '-' -> if (inClass) literal(45) else throw Invalid()
                in "\\.^$|?*+(){}[]/" -> literal(esc.code)
                else -> throw Invalid()
            }
        }

        private fun characterClass(): (Int) -> Boolean {
            val negative = take("^")
            val tests = mutableListOf<(Int) -> Boolean>()
            while (peek() != ']') {
                val left = element(true)
                if (peek() == '-' && source.getOrNull(pos + 1) != ']') {
                    pos++
                    val a = left.literal ?: throw Invalid()
                    val b = element(true).literal ?: throw Invalid()
                    if (a > b) throw Invalid()
                    tests.add { cp -> inFoldedRange(cp, a, b) }
                } else tests.add(left.test)
            }
            if (tests.isEmpty()) throw Invalid()
            pos++
            return { cp -> tests.any { it(cp) } != negative }
        }
    }

    companion object {
        private val lineTerminators = setOf(10, 13, 0x2028, 0x2029)

        private val otherWhitespace =
            setOf(32, 0xa0, 0x1680, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000, 0xfeff)

        private fun whitespace(c: Int) = c in 9..13 || c in 0x2000..0x200a || c in otherWhitespace

        private fun word(cp: Int) = fold(cp).let { it in 97..122 || it in 48..57 || it == 95 }

        // Pin the /iu case-fold data so older Android Unicode tables give the same results.
        private val folds by lazy {
            val stream =
                requireNotNull(
                    PortableRegex::class.java.getResourceAsStream("/pruner/simple-fold-16.txt")
                )
            stream.bufferedReader().useLines { lines ->
                lines
                    .filterNot { it.startsWith('#') }
                    .associate { line ->
                        line.substringBefore(' ').toInt(16) to line.substringAfter(' ').toInt(16)
                    }
            }
        }

        private fun fold(cp: Int) = folds[cp] ?: cp

        private val equivalents by lazy { folds.keys.groupBy(::fold) }

        private fun inFoldedRange(c: Int, low: Int, high: Int): Boolean {
            if (c in low..high) return true
            val key = fold(c)
            return key in low..high || equivalents[key]?.any { it in low..high } == true
        }

        private val cache =
            object : LinkedHashMap<String, PortableRegex?>(256, .75f, true) {
                override fun removeEldestEntry(
                    eldest: MutableMap.MutableEntry<String, PortableRegex?>?
                ) = size > 256
            }

        @Synchronized
        fun compile(source: String): PortableRegex? {
            if (source.codePointCount(0, source.length) > 512) return null
            if (cache.containsKey(source)) return cache[source]
            val result =
                try {
                    PortableRegex(Parser(source).parse())
                } catch (_: Invalid) {
                    null
                } catch (_: StackOverflowError) {
                    null
                }
            cache[source] = result
            return result
        }
    }
}
