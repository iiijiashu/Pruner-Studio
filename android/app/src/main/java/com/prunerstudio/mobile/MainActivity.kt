package com.prunerstudio.mobile

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.*
import androidx.activity.compose.*
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.*
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.*
import androidx.compose.material.icons.outlined.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.*
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.*
import androidx.core.splashscreen.SplashScreen.Companion.installSplashScreen
import androidx.core.view.WindowCompat
import androidx.media3.common.util.UnstableApi
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.*
import com.prunerstudio.mobile.core.player.PrunerPlaybackService
import com.prunerstudio.mobile.ui.screens.*
import com.prunerstudio.mobile.ui.state.*
import com.prunerstudio.mobile.ui.theme.*

class MainActivity : ComponentActivity() {
    private val vm: PrunerViewModel by viewModels()
    private var openStageRequest by mutableStateOf(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        installSplashScreen()
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        if (savedInstanceState == null) handle(intent)
        setContent {
            val dynamicColor = if (vm.dynamicCoverAccent) vm.extractedCoverColor else null
            PrunerTheme(vm.theme, vm.accent, vm.scale, dynamicAccent = dynamicColor) {
                val light = colors.theme.isPaper
                SideEffect {
                    WindowCompat.getInsetsController(window, window.decorView).apply {
                        isAppearanceLightNavigationBars = light
                        isAppearanceLightStatusBars = light
                    }
                }
                val audio =
                    if (Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_AUDIO
                    else Manifest.permission.READ_EXTERNAL_STORAGE
                val permission =
                    rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) {
                        if (it) vm.scan() else vm.notice("读取本机音频需要媒体权限")
                    }
                val notify =
                    rememberLauncherForActivityResult(
                        ActivityResultContracts.RequestPermission()
                    ) {}
                LaunchedEffect(vm.state.is_playing) {
                    if (
                        Build.VERSION.SDK_INT >= 33 &&
                            vm.state.is_playing &&
                            !vm.notificationRequested &&
                            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
                                PackageManager.PERMISSION_GRANTED
                    ) {
                        vm.notificationRequested = true
                        notify.launch(Manifest.permission.POST_NOTIFICATIONS)
                    }
                }
                PrunerShell(
                    vm = vm,
                    openStageSignal = openStageRequest,
                    onStageSignalConsumed = { openStageRequest = false },
                ) {
                    if (checkSelfPermission(audio) == PackageManager.PERMISSION_GRANTED) vm.scan()
                    else permission.launch(audio)
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handle(intent)
    }

    @androidx.annotation.OptIn(UnstableApi::class)
    private fun handle(intent: Intent) {
        if (intent.action == Intent.ACTION_VIEW && intent.data?.scheme == "pruner-lan") {
            vm.tab = Tab.SETTINGS
            vm.parsePair(intent.dataString.orEmpty())
            intent.data = null
        }
        if (intent.getBooleanExtra(PrunerPlaybackService.EXTRA_OPEN_STAGE, false)) {
            openStageRequest = true
            intent.removeExtra(PrunerPlaybackService.EXTRA_OPEN_STAGE)
        }
    }
}

private const val ROUTE_SEARCH = "search"

private fun Tab.route(): String = name.lowercase()

@Composable
fun PrunerShell(
    vm: PrunerViewModel,
    openStageSignal: Boolean = false,
    onStageSignalConsumed: () -> Unit = {},
    scan: () -> Unit = {},
) {
    val snack = remember { SnackbarHostState() }
    var overlay by remember { mutableStateOf<Overlay?>(null) }
    val navController = rememberNavController()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val currentRoute = backStackEntry?.destination?.route ?: vm.tab.route()
    val isSearchRoute = currentRoute == ROUTE_SEARCH

    val outlinedIcons =
        listOf(
            Icons.Outlined.Explore,
            Icons.Outlined.LibraryMusic,
            Icons.Outlined.History,
            Icons.Outlined.ContentCut,
            Icons.Outlined.Tune,
        )
    val filledIcons =
        listOf(
            Icons.Filled.Explore,
            Icons.Filled.LibraryMusic,
            Icons.Filled.History,
            Icons.Filled.ContentCut,
            Icons.Filled.Tune,
        )

    fun navigateToTab(target: Tab) {
        vm.tab = target
        val route = target.route()
        if (navController.currentDestination?.route != route) {
            navController.navigate(route) {
                popUpTo(navController.graph.findStartDestination().id) { saveState = true }
                launchSingleTop = true
                restoreState = true
            }
        }
    }

    fun openSearch() {
        if (navController.currentDestination?.route != ROUTE_SEARCH) {
            navController.navigate(ROUTE_SEARCH) { launchSingleTop = true }
        }
    }

    LaunchedEffect(openStageSignal, vm.state.current_track != null) {
        if (openStageSignal) {
            if (vm.state.current_track != null) {
                overlay = Overlay.STAGE
                onStageSignalConsumed()
            } else {
                kotlinx.coroutines.delay(3000L)
                onStageSignalConsumed()
            }
        }
    }

    // Keep vm.tab synchronized when system Back pops the Navigation Compose back stack
    LaunchedEffect(currentRoute) {
        if (currentRoute != ROUTE_SEARCH) {
            Tab.entries.find { it.route() == currentRoute }?.let { routeTab ->
                if (vm.tab != routeTab) vm.tab = routeTab
            }
        }
    }

    // Keep programmatic `vm.tab = ...` changes in sync with Navigation Compose once graph is attached
    val graphReady = backStackEntry != null
    LaunchedEffect(vm.tab, graphReady) {
        if (graphReady) {
            val targetRoute = vm.tab.route()
            val cur = navController.currentDestination?.route
            if (cur != null && cur != ROUTE_SEARCH && cur != targetRoute) {
                navController.navigate(targetRoute) {
                    popUpTo(navController.graph.findStartDestination().id) { saveState = true }
                    launchSingleTop = true
                    restoreState = true
                }
            }
        }
        if (vm.tab == Tab.HISTORY || vm.tab == Tab.DISCOVER) vm.refresh()
    }

    LaunchedEffect(vm) { vm.messages.collect { snack.showSnackbar(it) } }

    Box(Modifier.fillMaxSize()) {
        Scaffold(
            containerColor = colors.background,
            contentColor = colors.text,
            contentWindowInsets = WindowInsets.safeDrawing,
            snackbarHost = {
                SnackbarHost(snack) { data ->
                    Snackbar(
                        snackbarData = data,
                        containerColor = colors.primaryActionBg,
                        contentColor = colors.primaryActionFg,
                        shape = controlShape,
                    )
                }
            },
            bottomBar = {
                Column(
                    Modifier.background(colors.surface)
                        .windowInsetsPadding(
                            WindowInsetsSides.Bottom.let { bottom ->
                                WindowInsets.safeDrawing.only(bottom + WindowInsetsSides.Horizontal)
                            }
                        )
                ) {
                    MiniPlayer(vm) { overlay = it }
                    if (!isSearchRoute) {
                        HorizontalDivider(color = colors.borderSubtle)
                        Row(
                            Modifier.fillMaxWidth()
                                .background(colors.surface)
                                .padding(horizontal = 4.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Tab.entries.forEachIndexed { i, tab ->
                                val selected = vm.tab == tab
                                val ink = if (selected) colors.accentText else colors.secondary
                                Column(
                                    Modifier.weight(1f)
                                        .heightIn(min = 56.dp)
                                        .selectable(selected, role = Role.Tab) { navigateToTab(tab) }
                                        .padding(top = 6.dp, bottom = 6.dp),
                                    horizontalAlignment = Alignment.CenterHorizontally,
                                    verticalArrangement = Arrangement.spacedBy(2.dp),
                                ) {
                                    Box(
                                        Modifier.clip(RoundedCornerShape(12.dp))
                                            .background(
                                                if (selected) blend(colors.surface, colors.accent, 0.14f)
                                                else Color.Transparent
                                            )
                                            .padding(horizontal = 14.dp, vertical = 4.dp),
                                        contentAlignment = Alignment.Center,
                                    ) {
                                        Icon(
                                            if (selected) filledIcons[i] else outlinedIcons[i],
                                            null,
                                            Modifier.size(22.dp),
                                            tint = ink,
                                        )
                                    }
                                    Text(
                                        tab.label,
                                        color = ink,
                                        style = MaterialTheme.typography.labelMedium,
                                        fontWeight =
                                            if (selected) FontWeight.SemiBold else FontWeight.Medium,
                                        maxLines = 1,
                                    )
                                }
                            }
                        }
                    }
                }
            },
        ) { padding ->
            Column(Modifier.fillMaxSize().padding(padding)) {
                if (!isSearchRoute) {
                    Row(
                        Modifier.fillMaxWidth()
                            .background(colors.background)
                            .padding(start = 16.dp, end = 8.dp, top = 6.dp, bottom = 4.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.SpaceBetween,
                    ) {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            Text(
                                vm.tab.label,
                                style = MaterialTheme.typography.headlineMedium,
                                color = colors.text,
                            )
                            if (vm.desktop != null) {
                                Box(
                                    Modifier.size(7.dp)
                                        .clip(CircleShape)
                                        .background(
                                            if (vm.live == "已连接") colors.keep
                                            else colors.secondary.copy(alpha = 0.45f)
                                        )
                                )
                            }
                        }
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            if (vm.tab == Tab.DISCOVER || vm.tab == Tab.LIBRARY) {
                                Glyph(Icons.Outlined.Search, "全局搜索", tint = colors.text) {
                                    openSearch()
                                }
                            }
                        }
                    }
                    HorizontalDivider(color = colors.borderSubtle)
                }

                NavHost(
                    navController = navController,
                    startDestination = Tab.DISCOVER.route(),
                    modifier = Modifier.weight(1f).fillMaxWidth(),
                ) {
                    composable(Tab.DISCOVER.route()) { DiscoverScreen(vm) }
                    composable(Tab.LIBRARY.route()) {
                        LibraryScreen(vm, scan, onOpenSearch = { openSearch() })
                    }
                    composable(Tab.HISTORY.route()) { HistoryScreen(vm) }
                    composable(Tab.DECK.route()) { DeckScreen(vm) }
                    composable(Tab.SETTINGS.route()) {
                        SettingsScreen(vm, { overlay = Overlay.EQ }, { overlay = Overlay.SLEEP })
                    }
                    composable(ROUTE_SEARCH) {
                        SearchScreen(vm) {
                            navController.popBackStack()
                        }
                    }
                }
            }
        }
        PlayerOverlays(vm, overlay, { overlay = it }, { overlay = null })
    }
}
