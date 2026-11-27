package com.prunerstudio.mobile.core.player

import android.content.Context
import androidx.media3.common.util.UnstableApi
import com.prunerstudio.mobile.core.db.*
import com.prunerstudio.mobile.core.lan.*
import com.prunerstudio.mobile.core.model.*
import com.prunerstudio.mobile.core.netease.*
import com.prunerstudio.mobile.core.sources.CompositeAudioSourceResolver
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.withContext

internal suspend fun queueLanClient(
    account: String,
    tracks: List<Track>,
    pairing: () -> LanSyncClient?,
) =
    if (
        tracks.none(::requiresLanPairing)
    )
        null
    else
        withContext(Dispatchers.IO) {
            val lan =
                pairing()
                    ?: throw AudioResolutionException(AudioErrorCategory.LAN_UNPAIRED)
            if (lan.config.accountId != account)
                throw AudioResolutionException(
                    AudioErrorCategory.LAN_UNPAIRED,
                    "当前账号与已配对桌面不一致，请重新配对后再播放桌面曲目",
                )
            lan
        }

@UnstableApi
class PrunerAppContainer internal constructor(
    context: Context,
    val credentials: SecureCredentialStore = AndroidKeystoreCredentialStore(context),
    neteaseClient: NeteaseApiClient? = null,
    allowMainThreadDb: Boolean = false,
) {
    private val appContext = context.applicationContext
    val database by lazy { PrunerDatabase.create(appContext, allowMainThread = allowMainThreadDb) }
    val netease by lazy { neteaseClient ?: NeteaseApiClient(credentials) }
    val auth = AuthSessionCoordinator(credentials)
    val repository by lazy { PrunerRepository(database) }
    val lanSession by lazy { LanSessionCoordinator(credentials, repository, ::lan, ::installPairing) }
    val scanner by lazy { LocalMediaScanner(appContext, repository) }
    val audioCache by lazy { AudioCacheManager(appContext, activePairing = ::activeLan) }
    val equalizer by lazy { EqualizerController(appContext) }
    val sources by lazy {
        CompositeAudioSourceResolver(
            repository, credentials, netease, ::lan, cachedPairing = { pairingState.value },
        )
    }
    val playback = PlaybackController(appContext, this)
    @Volatile private var cachedLan: LanSyncClient? = null
    private val _pairingState = MutableStateFlow<PairedServerConfig?>(null)
    val pairingState: StateFlow<PairedServerConfig?> = _pairingState.asStateFlow()

    fun createRoutingDataSourceFactory(): RoutingDataSourceFactory =
        RoutingDataSourceFactory(
            context = appContext,
            pairing = ::activeLan,
            streamAccess = sources::accessForStream,
            allowExternalUri = sources::isAuthorizedExternalStream,
        )

    fun preferredQuality(): AudioQualityLevel {
        val name = appContext.getSharedPreferences("pruner_ui_prefs", 0).getString("quality", null)
        return AudioQualityLevel.entries.firstOrNull { it.name == name } ?: AudioQualityLevel.jymaster
    }

    fun activeLan(): LanSyncClient? = cachedLan?.takeUnless { it.isClosed }

    @Synchronized
    fun lan(): LanSyncClient? {
        val config = credentials.pairing ?: return null.also {
            val stale = cachedLan
            cachedLan = null
            stale?.close()
            _pairingState.value = null
        }
        val current = cachedLan
        if (current != null && current.config == config) {
            _pairingState.value = config
            return current
        }
        cachedLan = null
        current?.close()
        return LanSyncClient(config, credentials) { lanSession.revoked(it) }.also {
            cachedLan = it
            _pairingState.value = config
        }
    }

    @Synchronized
    private fun installPairing(next: PairedServerConfig?): LanSyncClient? {
        val previous = cachedLan
        val previousConfig = previous?.config ?: credentials.pairing
        credentials.pairing = next
        cachedLan = null
        previous?.close()
        _pairingState.value = next
        if (previousConfig != null && previousConfig != next) {
            audioCache.evictLanEntries(AudioCacheKey.desktopNamespace(previousConfig))
        }
        return lan()
    }

    companion object {
        @Volatile private var instance: PrunerAppContainer? = null

        fun get(context: Context): PrunerAppContainer =
            instance
                ?: synchronized(this) {
                    instance
                        ?: PrunerAppContainer(context.applicationContext).also { instance = it }
                }
    }
}
