package com.prunerstudio.mobile

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.ui.Modifier
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.unit.dp
import com.prunerstudio.mobile.core.model.LyricLine
import com.prunerstudio.mobile.core.model.ParsedLyrics
import com.prunerstudio.mobile.core.player.PlaybackProgress
import com.prunerstudio.mobile.ui.screens.SyncedLyricsView
import com.prunerstudio.mobile.ui.theme.AccentPreset
import com.prunerstudio.mobile.ui.theme.PrunerTheme
import com.prunerstudio.mobile.ui.theme.PrunerThemeId
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class LyricsAutoScrollTest {
    @get:Rule val ui = createComposeRule()

    @Test
    fun successiveProgrammaticScrollsFollowPlaybackWithoutManualHold() {
        val position = mutableLongStateOf(0L)
        val lyrics = ParsedLyrics(List(90) { LyricLine(it * 1000L, "Line $it") })
        ui.setContent {
            PrunerTheme(PrunerThemeId.dark, AccentPreset.sage) {
                Box(Modifier.height(180.dp)) {
                    SyncedLyricsView(
                        lyrics, PlaybackProgress(positionMs = position.longValue), 0L,
                        showTranslation = false, showRomaji = false, fullScreen = true,
                        onSeekMs = {},
                    )
                }
            }
        }
        ui.runOnIdle { position.longValue = 25_000L }
        ui.onNodeWithText("Line 25").assertIsDisplayed()
        ui.runOnIdle { position.longValue = 60_000L }
        ui.onNodeWithText("Line 60").assertIsDisplayed()
    }
}
