package com.prunerstudio.mobile

import androidx.compose.ui.semantics.SemanticsProperties
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createComposeRule
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.ui.state.*
import com.prunerstudio.mobile.ui.theme.*
import kotlinx.serialization.json.JsonPrimitive
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.robolectric.*
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], qualifiers = "w411dp-h891dp")
class PrunerUiFlowTest {
    @get:Rule val ui = createComposeRule()
    private lateinit var vm: PrunerViewModel

    @Before
    fun setup() {
        ui.runOnIdle { vm = PrunerViewModel(RuntimeEnvironment.getApplication()) }
        ui.setContent { PrunerTheme(vm.theme, vm.accent) { PrunerShell(vm) } }
    }

    private fun click(text: String) = ui.onAllNodesWithText(text).onLast().performClick()

    @Test
    fun defaultsToWarmPaperTheme() {
        ui.runOnIdle {
            assertEquals(PrunerThemeId.parchment, vm.theme)
            assertTrue(vm.theme.isPaper)
            assertEquals(DeckOrder.RANDOM, vm.deckOrder)
        }
    }

    @Test
    fun discoverSmartRadioDetentsAcceptZeroAndOneHundredPercent() {
        ui.onNodeWithText("心动电台").assertExists()
        click("0% 纯熟歌")
        ui.runOnIdle { assertEquals(0.0f, vm.exploreRatio, 0.0001f) }
        ui.onNodeWithText("纯熟歌回味").assertExists()
        ui.onNodeWithText("0%").assertExists()

        click("100% 纯新歌")
        ui.runOnIdle { assertEquals(1.0f, vm.exploreRatio, 0.0001f) }
        ui.onNodeWithText("纯新歌探索").assertExists()
        ui.onNodeWithText("100%").assertExists()
    }

    @Test
    fun deckShowsStubNoticeAndRandomOrderControls() {
        ui.runOnIdle {
            vm.deck =
                Deck(
                    PlaylistSummary(JsonPrimitive(99), "清理歌单"),
                    listOf(
                        Track(id = 404, song_id = 404, title = "歌曲 #404"),
                        Track(id = 405, song_id = 405, title = "正常歌曲", artist = "歌手"),
                    ),
                )
            vm.tab = Tab.DECK
        }
        ui.onNodeWithText("随机抽选").assertExists()
        ui.onNodeWithText("换一首").assertExists()
        ui.onNodeWithText("曲目已下架 / 仅存 ID").assertExists()
        click("换一首")
        ui.runOnIdle { assertEquals("正常歌曲", vm.deck.current?.title) }
    }

    @Test
    fun fiveDestinations() {
        listOf(
                "发现" to "听歌洞察",
                "音乐库" to "本机音频",
                "历史" to "筛选播放历史",
                "清理" to "选择待整理歌单",
                "设置" to "桌面局域网互联",
            )
            .forEach { (tab, expected) ->
                click(tab)
                ui.onNodeWithText(expected).assertExists()
                val nodes =
                    ui.onAllNodes(
                            SemanticsMatcher.keyIsDefined(SemanticsProperties.Text),
                            useUnmergedTree = true,
                        )
                        .fetchSemanticsNodes()
                assertTrue(nodes.isNotEmpty())
                for (n in nodes) for (value in n.config[SemanticsProperties.Text]) {
                    assertFalse(
                        value.text.codePoints().anyMatch {
                            it in 0x1f000..0x1faff || it in 0x2600..0x27bf || it == 0xfe0f
                        }
                    )
                }
            }
    }

    @Test
    fun deckReviewsFrozenDecisions() {
        ui.runOnIdle {
            vm.deck =
                Deck(
                    PlaylistSummary(JsonPrimitive(12), "测试歌单"),
                    (1..3).map { Track(id = it.toLong(), title = "曲目" + it) },
                )
            vm.tab = Tab.DECK
        }
        click("保留")
        ui.onNode(hasText("待移除") and hasClickAction()).performClick()
        ui.runOnIdle {
            assertEquals(listOf(true, false), vm.deck.decisions)
            assertEquals(1, vm.deck.staged.size)
        }
        click("撤销")
        ui.runOnIdle { assertTrue(vm.deck.staged.isEmpty()) }
        ui.onNode(hasText("待移除") and hasClickAction()).performClick()
        click("核对移除清单")
        ui.onNodeWithText("确认移除").assertExists()
        click("取消")
        ui.runOnIdle {
            assertEquals(listOf(true, false), vm.deck.decisions)
            assertFalse(vm.removing)
        }
        click("核对移除清单")
        click("确认移除")
        ui.waitUntil(10000) { vm.deck.staged.isEmpty() }
        ui.runOnIdle { assertEquals("曲目3", vm.deck.current?.title) }
    }

    @Test
    fun pairingRejectsInvalidAndShowsPinnedIdentity() {
        click("设置")
        val field = ui.onNode(hasSetTextAction() and hasText("配对链接"))
        field.performTextInput("invalid")
        click("解析链接")
        ui.onNodeWithText("配对链接无效", substring = true).assertExists()
        val fp = "ab".repeat(32)
        field.performTextReplacement(
            "pruner-lan://pair?v=1&tls=1&host=192.168.2.4&port=9443&code=123456&fp=" + fp
        )
        click("解析链接")
        for (value in listOf("192.168.2.4", "9443", "123456", fp.chunked(2).joinToString(":"))) ui
            .onNodeWithText(value)
            .assertExists()
        click("取消")
        ui.runOnIdle { assertNull(vm.desktop) }
    }

    @Test
    fun miniPlayerOpensLyricsStage() {
        val track = Track(id = 7, title = "夜曲", artist = "演奏者")
        ui.runOnIdle {
            vm.state =
                PlaybackSessionSnapshot(
                    current_track = track,
                    queue = listOf(track),
                    queue_index = 0,
                    duration_sec = 180.0,
                    position_sec = 8.0,
                )
            vm.lyrics = ParsedLyrics(listOf(LyricLine(0, "第一行"), LyricLine(5000, "此刻的歌词")))
        }
        ui.onNodeWithText("此刻的歌词").assertExists()
        click("夜曲")
        ui.onNodeWithContentDescription("收起").assertExists()
        ui.onAllNodesWithText("此刻的歌词").onLast().assertExists()
        ui.onNodeWithContentDescription("上一首").assertExists()
        ui.onAllNodesWithContentDescription("播放").onLast().assertExists()
    }
}
