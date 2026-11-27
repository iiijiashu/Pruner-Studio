package com.prunerstudio.mobile.core.engine

import com.prunerstudio.mobile.core.model.*
import kotlinx.serialization.json.booleanOrNull

sealed interface RuleEvaluationResult {
    data object Matched : RuleEvaluationResult

    data object NotMatched : RuleEvaluationResult

    data class Unevaluable(val reason: String) : RuleEvaluationResult
}

object RulesEvaluator {
    private val equality = setOf("equals", "not_equals")
    private val comparison = equality + setOf("lt", "lte", "gt", "gte")
    private val textOperators =
        equality + setOf("contains", "not_contains", "starts_with", "ends_with")
    private val decimal = Regex("""[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)""")

    fun matches(rule: PruningRule, track: Track, likedSongIds: Set<Long> = emptySet()) =
        evaluate(rule, track, likedSongIds)

    fun evaluate(rule: PruningRule, track: Track, likedSongIds: Set<Long> = emptySet()) =
        evaluateRule(rule, track, likedSongIds) == RuleEvaluationResult.Matched

    fun inspectRule(rule: PruningRule): String? {
        if (rule.id.isBlank()) return "Missing rule id"
        if (rule.match_clauses.isEmpty()) return "Empty rule"
        for (clause in rule.match_clauses) {
            val value = clause.value
            if (value.isNullOrBlank()) return "Missing value for ${clause.field}"
            val primitive = requireNotNull(clause.operand)
            val valid =
                when {
                    clause.field == "liked" ->
                        clause.operator in equality && value.lowercase() in setOf("true", "false")
                    !primitive.isString && primitive.booleanOrNull != null -> false
                    clause.field == "duration" ->
                        clause.operator in comparison &&
                            (!primitive.isString || decimal.matches(value)) &&
                            value.toDoubleOrNull()?.isFinite() == true
                    clause.field in setOf("title", "artist", "artists", "album", "availability") ->
                        clause.operator in textOperators ||
                            clause.operator == "regex" && PortableRegex.compile(value) != null
                    else -> false
                }
            if (!valid) return "Unsupported clause: ${clause.field} ${clause.operator}"
        }
        return null
    }

    fun evaluateRule(
        rule: PruningRule,
        track: Track,
        likedSongIds: Set<Long> = emptySet(),
    ): RuleEvaluationResult {
        inspectRule(rule)?.let {
            return RuleEvaluationResult.Unevaluable(it)
        }
        if (!rule.enabled) return RuleEvaluationResult.NotMatched
        if (
            track.availability.isNullOrBlank() &&
                rule.match_clauses.any { it.field == "availability" }
        )
            return RuleEvaluationResult.Unevaluable("Missing track availability")
        val any = rule.match_mode == "any"
        var matched = !any
        for (clause in rule.match_clauses) {
            val result =
                matchesClause(clause, track, likedSongIds)
                    ?: return RuleEvaluationResult.Unevaluable("Regex exceeded evaluation budget")
            matched = if (any) matched || result else matched && result
        }
        return if (matched) RuleEvaluationResult.Matched else RuleEvaluationResult.NotMatched
    }

    private fun matchesClause(c: RuleClause, t: Track, likedSongIds: Set<Long>): Boolean? {
        val value = requireNotNull(c.value)
        if (c.field == "liked") {
            val liked = t.liked || (t.id ?: t.song_id) in likedSongIds
            return (liked == value.equals("true", ignoreCase = true)) == (c.operator == "equals")
        }
        if (c.field == "duration") {
            val a = t.durationSeconds()
            val b = value.toDouble()
            return when (c.operator) {
                "equals" -> a == b
                "not_equals" -> a != b
                "lt" -> a < b
                "gt" -> a > b
                "lte" -> a <= b
                else -> a >= b
            }
        }
        val text =
            when (c.field) {
                "artist",
                "artists" ->
                    t.artists.filter(String::isNotEmpty).joinToString(" / ").ifEmpty { "未知歌手" }
                "album" -> t.album
                "title" -> t.title
                else -> requireNotNull(t.availability)
            }
        if (c.operator == "regex") return requireNotNull(PortableRegex.compile(value)).matches(text)
        val a = text.lowercase()
        val b = value.lowercase()
        return when (c.operator) {
            "equals" -> a == b
            "not_equals" -> a != b
            "contains" -> b in a
            "not_contains" -> b !in a
            "starts_with" -> a.startsWith(b)
            else -> a.endsWith(b)
        }
    }
}
