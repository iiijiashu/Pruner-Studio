package com.prunerstudio.mobile

import com.prunerstudio.mobile.core.engine.*
import com.prunerstudio.mobile.core.model.*
import kotlinx.serialization.json.*
import org.junit.Assert.*
import org.junit.Test

class RulesEvaluatorTest {
    private val track =
        Track(id = 7, title = "Night Song", artists = listOf("A", "B"), duration_ms = 90500)

    private fun rule(field: String, op: String, value: String) =
        PruningRule(
            "1",
            "test",
            match_clauses = listOf(RuleClause(field, op, JsonPrimitive(value))),
        )

    @Test
    fun desktopOperators() {
        for ((field, op, value) in
            listOf(
                Triple("title", "starts_with", "night"),
                Triple("title", "ends_with", "SONG"),
                Triple("title", "not_contains", "day"),
                Triple("artist", "equals", "a / b"),
                Triple("artists", "contains", "/"),
                Triple("title", "regex", "^(?:NIGHT|DAY) s[a-z]+$"),
                Triple("duration", "equals", "90.5"),
                Triple("duration", "gt", "90"),
                Triple("duration", "lt", "91"),
                Triple("duration", "not_equals", "90500"),
                Triple("duration", "lte", "90.5"),
                Triple("duration", "gte", "90.5"),
                Triple("liked", "equals", "true"),
                Triple("liked", "not_equals", "FALSE"),
            )) assertTrue(
            "$field $op $value",
            RulesEvaluator.matches(rule(field, op, value), track, setOf(7)),
        )
        val liked = rule("liked", "equals", "true")
        assertFalse(RulesEvaluator.matches(liked, track))
        assertTrue(RulesEvaluator.matches(liked, track.copy(liked = true)))
    }

    @Test
    fun unavailableDataAndInvalidClausesDisableEntireRule() {
        val good = rule("liked", "equals", "false")
        for (invalid in
            listOf(
                rule("title", "regex", "("),
                rule("title", "regex", "(?i)night"),
                rule("title", "regex", "a++"),
                rule("title", "regex", "\\1(a)"),
                rule("duration", "gt", "NaN"),
                rule("duration", "lt", "1e2"),
                rule("play_count", "gte", "0"),
                rule("availability", "not_equals", "available"),
            )) {
            val any =
                invalid.copy(
                    match_mode = "any",
                    match_clauses = good.match_clauses + invalid.match_clauses,
                )
            assertTrue(RulesEvaluator.evaluateRule(any, track) is RuleEvaluationResult.Unevaluable)
            assertFalse(RulesEvaluator.matches(any, track))
        }
        val availability = rule("availability", "equals", "available")
        assertTrue(RulesEvaluator.matches(availability, track.copy(availability = "available")))
        assertEquals(
            RuleEvaluationResult.NotMatched,
            RulesEvaluator.evaluateRule(availability, track.copy(availability = "unavailable")),
        )
    }

    @Test(timeout = 3000)
    fun regexBudgetStopsExcessiveBacktracking() {
        val expensive = rule("title", "regex", "^(a|aa)+$")
        val result =
            RulesEvaluator.evaluateRule(expensive, track.copy(title = "a".repeat(160) + "!"))
        assertNotEquals(RuleEvaluationResult.Matched, result)
    }

    @Test
    fun portableRegexExamples() {
        for ((pattern, title) in
            listOf(
                "(live)\\1" to "LiveLIVE",
                "^(?:live|demo)$" to "DEMO",
                "(a|b){2,3}?" to "AAb",
                "(?=live)live" to "Live",
                "[(?i)]+" to "?",
                "\\(\\?i\\)" to "(?i)",
            )) assertTrue(
            pattern,
            RulesEvaluator.matches(rule("title", "regex", pattern), track.copy(title = title)),
        )
        for (pattern in
            listOf(
                "(?i)live",
                "(?<name>live)",
                "\\Aabc",
                "[]",
                "a{3,2}",
                "\\1(live)",
                "\\p{Letter}",
                "[\\uD83D\\uDE00-\\uD83D\\uDE4F]",
            )) assertNotNull(pattern, RulesEvaluator.inspectRule(rule("title", "regex", pattern)))
    }

    @Test
    fun unicodeAndZeroWidthMatchesFollowDesktopRegExp() {
        for ((pattern, text, expected) in
            listOf(
                Triple("a$", "a\n", false),
                Triple(".", "\r", false),
                Triple("\\s", "\u00a0", true),
                Triple("\\w", "K", true),
                Triple("\\w", "ſ", true),
                Triple("ss", "ß", false),
                Triple("i", "İ", false),
                Triple("\\d", "١", false),
                Triple("(a)?b\\1", "b", true),
                Triple("^.$", "🎵", true),
                Triple("[a&&b]", "a", true),
                Triple("\\bK\\b", "K", true),
            )) assertEquals(
            pattern,
            expected,
            RulesEvaluator.matches(rule("title", "regex", pattern), track.copy(title = text)),
        )
    }

    @Test
    fun projectedPageSurvivesMissingFieldsAndRetainsNumbers() {
        val page =
            wire.decodeFromString<SyncPullResponse>(
                """{
          "account_id":"a","server_time":"now","sync_until":"now","history_has_more":false,
          "feedback_has_more":false,"play_history":[{"id":7}],"feedback":[],
          "playlists":[{"id":"1","kind":"self_created","track_count":2}],
          "rules":[{"name":"missing-id","match_clauses":[{"field":"title","operator":"contains","value":"a"}]},
                   {"id":"a","name":"broken","match_clauses":[{"field":"title","operator":"contains"}]},
                   {"id":"b","name":"valid","match_clauses":[{"field":"duration","operator":"gt","value":1e-5}]}]}"""
            )
        assertEquals("未命名歌单 #1", page.playlists!!.single().displayName())
        assertEquals(7L, page.play_history.single().id)
        assertNotNull(RulesEvaluator.inspectRule(page.rules[0]))
        assertTrue(RulesEvaluator.evaluateRule(page.rules[0], track) is RuleEvaluationResult.Unevaluable)
        assertNotNull(RulesEvaluator.inspectRule(page.rules[1]))
        assertFalse(RulesEvaluator.matches(page.rules[1], track))
        assertTrue(RulesEvaluator.matches(page.rules.last(), track))
        assertEquals("0.00001", page.rules.last().match_clauses.single().value)
        val unlabelled = wire.decodeFromString<Track>("""{"duration":90}""")
        assertEquals(90.0, unlabelled.durationSeconds(), 1e-6)
        assertFalse(RulesEvaluator.evaluate(rule("duration", "lt", "1"), unlabelled))
        assertTrue(RulesEvaluator.evaluate(rule("duration", "equals", "90"), unlabelled))
        assertTrue(
            RulesEvaluator.evaluate(
                rule("duration", "equals", "90"),
                Track(duration = 90.0, duration_unit = "sec"),
            )
        )
        assertTrue(
            RulesEvaluator.evaluate(
                rule("duration", "equals", "90"),
                Track(duration = 90.0, provider = "qq"),
            )
        )
        assertTrue(
            RulesEvaluator.evaluate(rule("duration", "equals", "90"), Track(duration = 90000.0))
        )
    }
}
