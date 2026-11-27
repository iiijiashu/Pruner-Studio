# Pruner Studio design contract

## Subject, audience, and single job

Pruner Studio (`0.4.0`) is a local-first **4-destination daily NetEase Cloud Music desktop client, multi-channel high-res audio hub, and safe playlist pruner** built for one person. It brings quiet everyday listening, local-first smart radio discovery, online-first paginated multi-channel search, verified cross-channel lossless playback, AMLL-grade immersive & transparent desktop lyrics, multi-theme customization, and a safety-first pruning desk into one calm desktop application.

The interface is a precision listening tool, not a portfolio page. “Simple premium” means restrained typography, coherent theme surfaces, real cover-led or user-chosen accents, crisp vector iconography, and measured transitions. It never uses decorative glassmorphism, generic blue SaaS dashboards, emoji-led headings, WebGL, cursor followers, or hidden efficiency controls.

The current Windows build hash, test totals and packaged application checks are recorded in [ACCEPTANCE.md](ACCEPTANCE.md#2026-11-22-windows-x64-本地结果).

## Visual system

### Multi-theme engine, palette & appearance tokens

Managed by `web/js/theme.js`, applied synchronously from `localStorage` (`pruner_appearance`) at module import to eliminate FOUC, and persisted through `PUT /api/settings { appearance }`:

- **Light-priority opaque multi-theme engine** (`html[data-theme]`):
  - All persistent chrome (`--bg-rail`, `--bg-header`, `--bg-player`) is **100% opaque (`alpha = 1.0`)** with crisp `1px` `--border-subtle` hairlines and zero `backdrop-filter` across every theme, preventing scrolled text bleed-through.
  - **Light-first paper anchors** (matte surfaces with `--surface-sheen: none`, recessed `--bg-well` trays, softened warm charcoal primary actions `--btn-primary-bg`, accent-harmonized play circle `--player-play-bg: var(--accent-text)`):
    - `parchment` (primary warm paper anchor): `#f3ede2` canvas, `#efe8db` opaque rail, `#fbf8f2` paper surface, `#ede5d7` recessed well, `#faf6ee` opaque player bar, `#241e17` warm sumi primary text (`>= 11.9:1`), `#584e42` secondary, `#5c5145` muted, softened `#3a3127` primary CTA with `#faf6ee` text (`11.8:1`).
    - `light` (crisp editorial paper): `#f5f3ee` canvas, `#efede7` opaque rail, `#fdfcfa` paper surface, `#eceae3` well, `#fcfbf8` opaque player bar, `#1b1a18` primary text (`>= 13.0:1`), `#54524c` secondary, `#5a5851` muted, softened `#33312d` primary CTA.
    - `celadon` (soft celadon washi): `#ebf0ee` canvas, `#e4ebe8` opaque rail, `#f7faf9` surface, `#e1e9e6` well, `#f5f9f7` opaque player bar, `#172420` ink (`>= 11.7:1`).
  - **Dark & dynamic companions** (100% opaque chrome, harmonized elevation):
    - `dark`: `#111215` canvas, `#141519` rail, `#18191e` working surface, `#15161b` recessed well, `#16171c` player bar, `#f2f1ed` Bone text, `#a0a2aa` Ash secondary, `#8e9099` muted.
    - `midnight`: OLED pure `#000000` canvas, `#08080a` rail, `#0d0d10` surface, `#0b0b0e` player bar, hairline separation without drop shadows.
    - `nordic`, `espresso`, and `emerald`: opaque deep blue (`#111822`), coffee (`#181412`), and pine (`#101714`) surfaces with recessed `--bg-well` trays.
    - `dynamic`: tints dark surfaces with the extracted album color (each channel capped at `<= 39` to preserve contrast).
    - `auto`: tracks OS `prefers-color-scheme` live (mirroring opaque `light` tokens in light mode).
  - **Soft hairline hierarchy**: resting search triggers (`.header-search-trigger`), recessed panels (`.settings-block`, `.deck-lyrics-wrapper`, `.discover-explore-control`), and inputs use `--bg-well` + `--border-subtle`/`--border-medium`, reserving high-contrast `--border-control` (`>= 4.5:1` WCAG AA) for checkboxes, switches, and focus indicators.
  - All `base.css` hex color tokens remain lowercase `#rrggbb`, and all `web/css/*.css` font sizes stay `>= 12px` (`0.75rem`, zero `10px`/`11px`).
- **Accent system** (`--accent`, `--accent-subtle`, `--accent-contrast`):
  - Six curated presets (`鼠尾草 #62a368`, `钴蓝 #4c8bf5`, `紫罗兰 #8b6ce8`, `琥珀 #d99e4e`, `珊瑚 #e07a5f`, `玫瑰 #c86b85`), a custom hex picker (`<input type="color">`), and dynamic `cover` mode driven by real 4-quadrant cover palette extraction.
  - **Immutable semantic invariants**: Keep `#62a368` (`--color-keep`) and Prune `#df584c` (`--color-prune`) are fixed across themes and accents. Both solid buttons use `#121214` text, giving contrast ratios of 6.20:1 and 5.02:1 respectively.
- **Density & typography scale**:
  - Density (`html[data-density]`): `comfortable` (default) or `compact`.
  - Font scale (`--font-scale` on `html { font-size: calc(16px * var(--font-scale)); }`): `90%` (`0.9`), `100%` (`1.0`), `110%` (`1.1`), or `125%` (`1.25`).
- **4-Quadrant Real Cover Palette Extraction**: fetch a `64×64` same-origin cover through `/api/cover?url=...&size=64` (graceful fallback when unavailable), sample 4 quadrant dominant colors (`c1..c4`) on an offscreen canvas normalized for richness and readability, and expose `--cover-accent`, `--cover-accent-subtle`, and fluid canvas mesh colors `c1..c4`.

### Typography and iconography

- Chinese and body UI: `"Segoe UI Variable Text", "HarmonyOS Sans SC", "MiSans", "PingFang SC", "Microsoft YaHei UI", "Noto Sans CJK SC", sans-serif`.
- Utility numerals, timers, bitrates, percentages, and sample rates: `"Cascadia Mono", "SFMono-Regular", Consolas, monospace` with `font-variant-numeric: tabular-nums` (never apply monospace styling to Chinese text sentences).
- Icons: crisp inline `1.5px` stroke SVG icons constructed via DOM (`createElementNS`, zero `innerHTML`, zero emojis, no crude text glyphs like `▶` or `×` for primary controls; disc/radio SVG gradient fallbacks replace placeholder letters).

The Windows launcher requests PerMonitorV2 before the app imports WebView2. The packaged manifest declares the same context for mixed-DPI displays. Headings use the body font stack.

### Geometry, discrete covers, toasts, and controlled dynamic motion

- Spacing scale: `4, 8, 12, 16, 24, 32, 48px`.
- Control radius: `6px`. Surface radius: `10px`. Never use pill shapes for ordinary controls.
- Discrete cover sizing: append `?param={size}y{size}` with strictly bounded tiers for NetEase `126.net` URLs (`96` for rows/queue/bottom bar, `192` for catalog cards/discover tiles, `384` for review cover and immersive stage; `64` via `/api/cover` for palette sampling).
- Fast feedback: `120ms`. Standard transition: `180ms`. Song/cover crossfade: `220–260ms`. Fluid color lerp: `600ms`.
- Visible keyboard focus: `2px` `--accent` outline plus separation ring.
- **Toast discipline (`toast.js`)**: cap at 3 visible toasts (evicting the oldest), 2-second duplicate suppression, and `toast.quiet()` (1.2s lifetime, max 1 quiet toast stacked) for routine volume, offset, and play-mode changes.
- **Two visual modes** (configurable in `偏好设置`):
  - `柔和动态` (`soft_dynamic`, default): regular views use an ultra-lightweight static top radial tint with a `220ms` opacity crossfade on track change (no full-viewport `filter: blur(100px)` and no `transition: background-image`). Inside the on-demand **Immersive Stage** only, mount a single **`160×160` 2D Canvas fluid oil-paint mesh gradient** (20fps balanced 4-color blend + 600ms color lerp + subtle 3% SVG noise layer).
  - `静谧省电` (`quiet_eco`): disables fluid canvas animation and collapses decorative transitions to minimize GPU/power usage.
- In balanced mode the radial mesh updates at 20 fps and the spectrum at 30 fps. The 160×160 offscreen mesh blends radial gradients before display. The spectrum has a 320×104 CSS-pixel budget and a maximum pixel ratio of 1.5.
- High mode permits 60 fps during visible, focused stage playback. Eco mode uses a static gradient and disables animation and cover tilt. Hiding the window, minimizing, losing focus, pausing playback or requesting reduced motion stops animation. Closing the stage clears its canvas backing stores.
- The stage switcher persists four layouts. Fluid follows the album palette with a pastel variant in light mode. Vinyl pairs a grooved record with a pivoting tonearm. Poster centers the artwork and two focus lines. Visualizer samples the Web Audio analyser for mirrored bars, a ring or a perspective grid. Silent and direct-source audio settle to a baseline.
- Same-origin stream decks have independent gains before the shared equalizer, compressor, master gain and analyser. The incoming playing event starts the overlap. Pause, interrupted loads and logout release the outgoing deck.

## Signature: AMLL immersive stage & transparent desktop karaoke lyrics

In regular views, a quiet top-edge cover tint anchors the screen to the current track without competing with lists or pruning decisions. Clicking the bottom player cover opens the **Full-Screen Immersive YRC Lyrics Stage**:

- **Fluid canvas, cover breathing & desktop geometry**: renders the single bounded `160×160` 4-color oil-paint mesh canvas over the active theme, using a balanced `38% / 62%` desktop split (`>=1200px`) with crisp `384px` cover (Apple Music-style `scale(1)` + subtle `--bass-glow` while playing, `scale(0.94)` paused, `disc` SVG gradient fallback), left-aligned lyrics (`max-width: 720px`), verified quality/source badge, AMLL TTML pill when `ttml_source` is active, and a compact top-right lyric offset stepper (`− | 0.0s | +`; clicking the value resets to `0.0s` with `toast.quiet`).
- **Stage lifecycle**: the top-left `收起` button (with down-chevron icon) or `Esc` closes the stage, cancels `requestAnimationFrame`, unmounts the fluid canvas, and restores focus to the trigger control. Opening mid-song immediately renders current lyrics, highlights and scrolls to the current playback position, and starts YRC `requestAnimationFrame` sync when playing.
- **Zero-reflow distance-graded YRC/TTML lyrics**:
  - Parses standard LRC (`[mm:ss.xx]`), NetEase word-by-word YRC (`[lineStart,lineDur](wordStart,wordDur,0)word`), translations (`tlyric`), romaji (`romalrc`), and zero-Cookie `amll-ttml-db` fallback (DTD/XXE-safe TTML-to-YRC conversion when NetEase lacks word timings and `enable_amll_ttml_fallback` is enabled).
  - Distance-graded opacity (`data-distance="0|1|2|far"`, lifted cleanly on hover), optional `lyrics_blur_depth` blur on ±3 lines, spring-damped `cubic-bezier(0.22, 1, 0.36, 1)` motion with subtle active glow, and a pure HTML/CSS 3-dot breathing indicator for `>5s` instrumental interludes (zero emojis).
  - Lyrics keep their raster size and use color, weight and opacity to distinguish the active line.
- **Transparent Desktop Floating Karaoke Lyrics (`web/desktop_lyrics.html`)**:
  - Optional frameless always-on-top resizable transparent window (`transparent=True`, default `820×120` bottom-center with single retry fallback to `#141518` if unsupported; debounced 500ms `moved`/`resized` persistence in `desktop_lyrics_bounds`; transparency-safe Win32 `WS_EX_TRANSPARENT | WS_EX_LAYERED` click-through lock without `SetLayeredWindowAttributes` clobbering).
  - Desktop lyrics use five palettes, soft shadows and an optional 0.7px stroke. Win32 applies tool-window and control-box styles to the existing HWND so WebView2 keeps its controller. An unlocked left press on the lyric area starts native dragging. The hover toolbar sends transport and offset commands, opens the main settings panel, cycles palettes and waits for native close acknowledgement. A locked window is transparent and click-through.
  - Playback SSE supplies sequence, offset, rate, buffering and heart state. Word interpolation stops on pause, buffering and hidden documents. Balanced and high modes cap painting at 30 and 60 fps. Eco paints only when an event arrives.

## Layouts

### Login & offline entry

```text
┌────────────────────────────────────────────────────────────┐
│ PRUNER STUDIO                                      本机运行 │
│                                                            │
│  让曲库只留下                 ┌─────────────────────────┐   │
│  你还会再听的歌。             │        QR CODE          │   │
│                               │                         │   │
│  [进入离线本地曲库 (如有缓存)] │  等待扫码 / 手机确认    │   │
│  private listening desk       │  重新生成               │   │
│                               └─────────────────────────┘   │
└────────────────────────────────────────────────────────────┘
```

When `GET /api/status` reports unauthenticated or offline status and local `cached_accounts` exist, the login screen offers a direct read-only offline library entry instead of trapping the user behind the QR wall.

### Application shell: 4-destination collapsible rail + global bottom player

```text
┌──────────────┬───────────────────────────────────────────────┐
│ PRUNER       │ [≡] Ctrl+K 全局搜索 ─────── user · sync state │
│ STUDIO       ├───────────────────────────────────────────────┤
│              │                                               │
│ ▸ 发现       │   Active view                                 │
│   音乐库     │   (发现 / 音乐库 / 歌单清理 / 设置与同步       │
│   歌单清理   │    + on-demand 全页搜索 #view-search)          │
│   设置与同步 │                                               │
│              │                                               │
├──────────────┴───────────────────────────────────────────────┤
│ [Cover↗] Title·Artist [徽标] ♫单行歌词 ♡ │ ⏮ ▶ ⏭ ══ │ [词][更多] 队列 🔊 │
└──────────────────────────────────────────────────────────────┘
```

- **Collapsible left rail**: `208px` expanded on desktop (`224px` at `>= 1920px`), `64px` compact icon mode, and automatically collapses into a slide-over drawer at narrow widths (`<= 1024px`, covering `1024×768` at 125%/150% Windows DPI scaling) with zero horizontal overflow and all controls reachable. The rail header carries a quiet single-line uppercase studio colophon (`13px` secondary ink, avoiding double-branding echo beneath the native OS title bar), while the `本机运行` status indicator (`.rail-status-bar > .brand-badge`) anchors quietly at the bottom of the rail. The `Ctrl+K` search trigger sits in the opaque top utility header on a recessed `--bg-well` hairline surface.
- **Persistent global bottom player (`84px` comfortable / `74px` compact)**: remains mounted across all views on an opaque `--bg-player` console with `>= 8px` vertical clearance below `#player-inline-lyric`, a harmonized `38px` play circle (`--player-play-bg` / `--player-play-fg`), crisp unblurred `--bg-track` scrubber/volume meters, and an inline tabular queue pill (`.player-queue-btn > .queue-count-dot`, `position: static`, zero overlap with the queue SVG). Owns the single active `<audio>` transport (protected by `resetMediaElement` and dual-playback suppression), compact inline current lyric (`#player-inline-lyric`, hidden while stage is open), explicit `词` desktop-lyrics button (`#player-desktop-lyrics-btn` with `.active` state), `更多` overflow popover (`#player-more-btn` housing `-5s`, `+5s`, chorus jump, similar songs, switch audio source, side lyrics, and stage), unified play queue with Fiedler Balanced Shuffle + `playHistoryStack`, safe same-origin Web Audio graph, and Media Session / Windows global hotkey integration.

### Destination 1: `发现` (Discover)

```text
┌─ 心动电台 (1fr) ─────────────┬─ 私人漫游 (1fr) ─────────────┐
│ [熟歌 ━━●━━ 探索 37%] (step=1%) │ [108×108 Cover] Play/Next/♡  │
│ 4路混合本地优先电台 · 一键开播   │ 经典原生只读漫游              │
├─────────────────────────────────┴──────────────────────────────┤
│ 本地听歌洞察：总播放 · 总时长 · 沉睡宝藏数 · Top 5 歌手 · 音质  │
├────────────────────────────────────────────────────────────────┤
│ 沉睡宝藏（横向卡片条，最多12首，>30天未听 / 从未播放）[播放全部]│
├────────────────────────────────────────────────────────────────┤
│ 每日推荐 (30首) [播放全部] — Track rows w/ 推荐理由 + 不再推荐  │
│ 最近播放 / 相似歌曲入口（支持右键菜单）                          │
└────────────────────────────────────────────────────────────────┘
```

- Equal-width `1fr 1fr` 2-column listening hero (`私人漫游` + `心动电台`, matching surface elevation, `--shadow-card`, `10px` card radius, `6px` cover radius, recessed `--bg-well` explore slider tray, no English `"Personal FM"` subtitle). The Smart Radio explore slider moves continuously at `step="1"` (`20%–80%`), updates `探索 N%` (`tabular-nums`) and `--fill` live, and debounces `api.saveSettings` by 300ms.
- **Toolbar weight budget & Discover hygiene**: section toolbars (`沉睡宝藏`, `每日推荐`) enforce at most **1 primary CTA + 1 secondary CTA + hairline divider + uniform quiet ghost actions**. `沉睡宝藏` renders as a scrollbarless horizontal strip (`scrollbar-width: none`, hidden webkit scrollbar) with dynamic edge paper-fade masks and `#discover-dusty-prev` / `#discover-dusty-next` chevron steppers with honest disabled bounds. `每日推荐` rows suppress redundant generic `"网易云每日推荐"` / `"每日推荐"` pills (displaying only distinctive editorial reasons) and progressively reveal secondary row actions (`下一首`, unliked heart, add-to-playlist, icon-only `不再推荐` with `.sr-only` label) on `:hover` / `:focus-within` while keeping active liked hearts and right-aligned `tabular-nums` durations calm and steady.
- **4-Way Local-First Smart Radio** (`GET /api/recommend/smart-radio`): blends Dusty Gems (30%) + Fresh Rotation (30%) + Multi-Seed `/simi/song` Graph Intersection (25% across 3–5 diverse seeds) + FM/Daily Exploration (15%), modulated by `explore_ratio`, filtering DJ/cover/karaoke and `not_interested`, spread via Martin Fiedler balanced shuffle, auto-refilling when `<=3` tracks remain, with honest reason tags.

### Destination 2: `音乐库` (My Library / Catalog)

```text
┌ 我喜欢的音乐 | 自建歌单 | 收藏歌单 | 网易云云盘 | 本地曲库 | 离线导入 ┐
├ 搜索 ─ 筛选 ─ 排序 ─ [表格|卡片] ─ 规则抽屉 ─ [播放全部] [进入清理]   ┤
├ 勾选操作栏（仅在有选中项时显示）                                      ┤
│ 表格：紧凑行，50首/页，O(1) 增量勾选，右键上下文菜单                  │
│ 卡片：五列紧凑卡片，离散 ?param=192y192 封面                          │
└───────────────────────────────────────────────────────────────────────┘
```

- `收藏歌单` (subscribed) and `离线导入` (imported) are read-only for playback/browsing and never expose remote removal writes.
- Rules live in a catalog drawer with precompiled regexes; rules only preview, select, or stage into batch confirmation — they never execute cloud writes directly.

### Destination 3: `歌单清理` (Pruner Deck)

```text
┌ 歌单 | 筛选(未决/稍后再听/已跳过/规则/VIP/不可用/时长) | 排序 | 重置 ┐
├ 进度 128/500 ───────────────────────────── 剩余 372 · 约 18 分钟 ────┤
│ ┌─ fixed left ───┐  ┌─ flexible right pane ────────────────────────┐ │
│ │ 320–380px      │  │ TITLE · artist / album / timing / badge      │ │
│ │  ALBUM COVER   │  │ ┌─ 未在播放 · 按 P 试听 [试听此曲] (不遮挡) ─┐ │ │
│ │  (1:1 square)  │  │ │ fixed-height zero-reflow YRC/LRC lyrics  │ │ │
│ │  (384px)       │  │ │ (mask-image on scroller only)            │ │ │
│ └────────────────┘  └─┴──────────────────────────────────────────┴─┘ │
├ [立即同步 3s | 批量确认] ── 居中: 移除·撤销·稍后再听·跳过·播放·副歌·保留 ┤
└──────────────────────────────────────────────────────────────────────┘
```

- Deck playback always uses `{ preserveQueue: true, origin: 'deck' }` so auditioning tracks in `歌单清理` never clobbers the global listening queue and stops cleanly on track end.
- When `deckSong.id !== currentPlayingSong.id`, the Deck fetches & caches `api.getLyrics(deckSong.id)` (cancelling stale async responses) and displays static own-song lyrics under a normal-flow `.deck-lyrics-header` (`未在播放 · 按 P 试听` + `试听此曲` button) that never overlaps lyric line 1.
- `#deck-mode-toggle` is a clear 2-option segmented control (`立即同步 3s | 批量确认`); all 7 required `deck-*` control hooks remain directly visible and reachable.

### Destination 4: `设置与同步` (Settings & Sync)

```text
┌ [偏好设置] | [多渠道与会员] | [同步与备份] ───────────────────────────┐
├ 偏好设置 tab:                                                         ┤
│  外观卡片：主题(七种主题/跟随系统) · 强调色(6预设/自定义/封面)   │
│            界面密度(舒适/紧凑) · 字体缩放(90%–125%) · 视觉模式        │
│  音质阶梯 · 响度均衡 · 交叉淡入淡出(150ms) · 歌词虚化/低频光/AMLL回退  │
│  桌面悬浮歌词开关/穿透锁定 · Windows全局热键 · LRU缓存 · 本地无损目录  │
├ 多渠道与会员 tab:                                                     ┤
│  音质仲裁模式：[全渠道最高音质择优] / [网易云原曲优先]                 │
│  渠道卡片：单行连接/VIP/加密状态 + 主操作[测试连通性] + 折叠[高级凭据] │
├ 同步与备份 tab:                                                       ┤
│  歌单同步与实时进度(SSE自动重连) · 待执行队列与只读核对恢复            │
│  备份快照 · 本地导入 · 操作记录(最近100条)                             │
└───────────────────────────────────────────────────────────────────────┘
```

- Uses a 3-tab top switcher (`偏好设置` / `多渠道与会员` / `同步与备份`) with the recovery banner pinned above all tabs when an unknown write requires attention.
- Channel cards collapse raw `Token / User ID / DFID / Cookie` inputs inside a closed-by-default `<details>` (`高级凭据`) disclosure, never returning or displaying raw secrets (showing `已加密保存`).

### Online-first `Ctrl+K` palette & full-page `#view-search`

- `Ctrl+K` opens on `'all'` (`全渠道聚合`) by default (persisting last channel in `pruner_search_channel`), displays a compact max-5 `曲库中` local match strip above online results from `api.searchMulti(q, channels, 30, 0)`, and provides a footer action (`查看全部结果 (空选 Enter / Shift+Enter)`) that opens `#view-search`.
- `#view-search` (`web/js/search_view.js`, `web/css/search.css`) provides a query bar, channel filter chips, sort (`相关度 | 音质优先`), **100 results per page** with `加载更多` (`offset` & `has_more`), channel/quality badges, double-click queue playback, direct cross-channel playback, right-click `openSongContextMenu`, and partial-channel error/empty/offline states.

### Unified right-click context menu & shortcuts help

- `web/js/context_menu.js` mounts a viewport-clamped, keyboard-navigable (`ArrowUp/Down/Home/End/Enter/Esc`) menu into `#context-menu-host` with `播放`, `下一首播放`, `加入歌单`, `喜欢·取消喜欢`, `不再推荐` (`discover` only), `查看相似`, and `复制歌名-歌手`.
- Pressing `?` outside inputs/modals opens `#shortcuts-help-modal` summarizing playback, navigation, global hotkeys, and Deck shortcuts.

### Pending drawer & frozen confirmation modal

```text
┌ 待执行 4 ───────────────────────────────┐
│ 即将提交 (3 秒)                         │
│  Song A · 取消喜欢          02.4s  取消 │
│ 稍后确认                                 │
│  喜欢的音乐 · 2 首                 移除 │
│  Road Trip · 1 首                  移除 │
└──────────────────────────────────────────┘

┌ 确认云端变更 ───────────────────────────┐
│ 取消喜欢                         12 首   │
│ 从 Road Trip 移出                 4 首   │
│ 已锁定当前清单快照与会话批次              │
│ 自动快照将在执行前创建                    │
│                         取消  确认执行   │
└──────────────────────────────────────────┘
```

Opening the batch confirmation modal freezes the exact manifest (`manifest_hash` + active `session_generation`), guaranteeing the executed cloud write matches what the user approved.

## Audio, source & interaction contract

- **VIP/SVIP quality ladder & Best-Quality Cross-Channel Arbitrator**:
  - Ladder: `jymaster -> hires -> lossless -> exhigh -> standard` (`sky` experimental). Display only backend-verified format, bitrate, and sample rate; never fabricate quality badges.
  - Arbitration (`quality_arbitration`): if NetEase original is already verified lossless/Hi-Res/master full track, return immediately with zero extra latency. If unavailable/trial — or if NetEase yields only lossy (`<500kbps` MP3/AAC) under `best_quality_race` — concurrently check User Cloud Disk, Local Lossless Folder, and enabled Multi-Channel Providers (`kugou_lite`, `migu`, `qq`, `kuwo`, `bilibili`, `gdstudio`, `custom`) and pick the strictly matching verified highest-quality full track via `score_audio_quality`, recording `quality_upgraded_from` when upgraded.
  - Manual **切换音源版本** modal (`/api/audio/candidates`) exposes NetEase and external candidates (including Live/remaster/cover).
- **Per-channel credential isolation & audio cache**:
  - `ChannelCredentialVault` encrypts per-channel credentials via Windows DPAPI and enforces `validate_custom_external_api` SSRF validation on every `api_base`. Each provider only receives its own credentials and strips them on cross-host redirects; NetEase cookies never reach external providers; `gdstudio` sends zero credentials; `custom` never receives NetEase credentials.
  - `/api/stream/{token}` streams local files and validated remote streams with HTTP Range (`200/206/416`) and atomically caches verified `200` audio MIME streams into `AudioDiskCache` (bounded LRU up to `audio_cache_limit_mb`, `0` disables caching; never caches HTML/error responses).
- **P0 ghost-audio prevention & safe Web Audio pipeline**:
  - Media elements are reset exclusively through `resetMediaElement(el)` (`pause` + `removeAttribute('src')` + `load`; zero `.src = ''` assignments). Error listeners ignore stale/unmatched `activeSrc` or loading states; `handleAudioElementError` is single-flight and `requestSequence`-guarded; `timeupdate` suppresses any accidental dual playback across `directAudio` and `streamAudio`.
  - `MediaElementAudioSourceNode -> DynamicsCompressorNode (loudness_normalization) -> GainNode (crossfade_ms, default 150ms) -> AnalyserNode (--bass-glow) -> destination` attaches ONLY to same-origin `/api/stream/{token}` URLs. Direct cross-origin URLs MUST bypass `MediaElementAudioSourceNode` to prevent silent CORS muting.
- **Parallel media resolution & chorus normalization**:
  - Fetch audio and lyrics concurrently; if playback resolves to a different `audio_id`, fetch lyrics for the resolved `audio_id` (falling back to `song.id`, and to zero-Cookie `amll-ttml-db` if NetEase lacks YRC and `enable_amll_ttml_fallback` is enabled). Normalize chorus timestamps explicitly from ms to sec; if absent, label the jump clearly as `跳至约 35%`.
- **Keyboard, global hotkeys & pruning rules**:
  - Deck shortcuts (`←/A` destructive, `→/D` keep, `Space` skip-to-end, `P` play/pause preview, `C/↑` chorus, `Z` undo newest uncommitted soft commit, `L` persistent relisten-later `稍后再听`) are active **only** in `歌单清理` and strictly ignore `e.repeat` and `e.isComposing` (IME).
  - Global in-app shortcuts (guarded against inputs, modals, palette, repeat, IME): `Space` play/pause outside `歌单清理`, `Ctrl+K` multi-channel search palette, `Ctrl+ArrowLeft/Right` prev/next, `Ctrl+ArrowUp/Down` volume ±5%, `?` shortcuts help, `Esc` close immersive stage/modals/palette/context menu and restore focus.
  - Optional Windows global hotkeys (`global_hotkeys_enabled`): `Ctrl+Alt+Right` (next), `Ctrl+Alt+Left` (prev), `Ctrl+Alt+P` (play/pause), `Ctrl+Alt+L` (toggle like), `Ctrl+Alt+D` (toggle desktop floating lyrics).
  - New accounts resolve the real `kind=liked` playlist ID (never a synthetic ID). Single destructive actions default to visible instant mode with a 3-second cancellable countdown; closing the app cancels soft commits, while internal navigation preserves them.
  - In liked music, destructive means unlike; in an owned playlist, it means remove from that playlist. Subscribed and local imported items cannot trigger cloud removal.
  - Unknown remote writes freeze destructive operations and never resolve prematurely on transient or truncated reads; reconciliation requires a complete, fresh remote snapshot or explicit user ignore with a durable tombstone.
  - Progress is stored per account + playlist + filter + order (including `L` relisten-later), with an explicit reset.
  - Table and card views share search, filter, sort, 50/page pagination, and O(1) selection state; changing search/filter/playlist clears hidden selections with a notice.

## Module direction

- Core state, theme & API: `theme.js` (FOUC-free synchronous appearance engine), `state.js` (account-scoped state, `session_generation`, queue, `playHistoryStack`, settings, visual mode), `api.js` (envelope validation for all routes including paginated `searchMulti` & `desktopCommand`), `auth.js` (QR lifecycle & offline cached-account entry), `utils.js` (safe DOM helpers, `getSizedCoverUrl`, 4-quadrant cover palette extraction, Fiedler shuffle fallback), `icons.js` (1.5px stroke SVG factory), `context_menu.js` (unified right-click song menu).
- Global playback, search, lyrics & desktop bridge: `player.js` (`resetMediaElement` P0 ghost-audio protection, Deck queue isolation, unified queue, safe same-origin Web Audio graph, Fiedler balanced shuffle, genuine listen recording, inline lyric, `词` toggle, `更多` popover, Media Session, SSE hotkey/desktop-lyrics bridge, direct cross-channel playback, version picker, expired-URL single retry), `lyrics.js` (LRC + YRC + AMLL TTML distance-graded/interlude/offset/static-deck zero-reflow engine), `stage.js` (on-demand full-screen immersive stage with `160×160` 2D Canvas fluid mesh, cover breathing & offset stepper), `search_palette.js` (online-first `Ctrl+K` palette with max-5 local strip), `search_view.js` (full-page `#view-search` 100/page paginated search), `web/desktop_lyrics.html` (transparent karaoke floating lyrics window).
- Four destinations: `discover.js` (equal 2-col `心动电台` continuous slider + `私人漫游` + `沉睡宝藏` horizontal strip + `本地听歌洞察` + `每日推荐`), `catalog.js` (+ `rules.js` drawer), `deck.js` (non-clobbering preview + own-song static lyrics + segmented mode toggle), `cloud.js` (3-tab `偏好设置` / `多渠道与会员` / `同步与备份`, SSE exponential backoff reconnect, collapsed `高级凭据` disclosures, `import.js`, frozen manifest confirmation via `modal.js` & `toast.js`).
- Stylesheets: `base.css`, `main.css`, `components.css`, `player.css`, `discover.css`, `catalog.css`, `deck.css`, `cloud.css`, `rules.css`, `stage.css`, `search.css`.

## Acceptance checklist

- 4-destination collapsible rail (`发现` / `音乐库` / `歌单清理` / `设置与同步`), dedicated `#view-search`, equal 2-col `发现` hero (`心动电台` 1% continuous slider + `私人漫游`) + 12-card `沉睡宝藏` + `本地听歌洞察`, 3-tab `设置与同步` (`偏好设置` w/ 外观 card / `多渠道与会员` w/ collapsed `高级凭据` / `同步与备份`), and persistent global bottom player (`词` + inline lyric + `更多` overflow) render cleanly at `2560`, `1920`, `1440`, `1280`, and `1024×768` (including 125%/150% DPI scaling) with zero horizontal overflow.
- All seven themes plus automatic system selection, 6 accent presets + custom hex + `cover` mode, density (`comfortable` / `compact`), and font scale (`90%–125%`) apply synchronously without FOUC while keeping Keep `#62a368` and Prune `#df584c` immutable and light theme contrast WCAG `>=4.5:1`.
- Zero `.src = ''` / `.src = ""` exists in `web/js/player.js`; rapid `playSong` switches keep at most one playing media element with zero ghost retries; Web Audio loudness/crossfade skips direct cross-origin URLs.
- Deck preview playback preserves the global listening queue and displays non-overlapping own-song static lyrics (`未在播放 · 按 P 试听`) when mismatched with the globally playing track.
- `Ctrl+K` defaults to `'all'` with max-5 local matches above online results; `#view-search` paginates 100/page with `offset` and `has_more`; right-click context menu works cleanly across rows.
- Immersive stage opens cleanly mid-song with `160×160` fluid canvas, `38% / 62%` desktop grid, and `− | 0.0s | +` offset stepper, closes via `收起` or `Esc` unmounting canvas & rAF, and pauses on blur/hidden/paused/`prefers-reduced-motion`/`静谧省电`.
- Transparent Desktop Floating Lyrics (`web/desktop_lyrics.html`) renders crisp stroked YRC karaoke sweep (`computeWordProgress`), persists style (`desktop_lyrics_style`) and bounds (`desktop_lyrics_bounds`), and dispatches allowlisted hover commands via `POST /api/desktop/command`.
- Toast notifications stay capped at 3 visible with 2s dedupe and single-slot `toast.quiet()`; SSE reconnects with exponential backoff and cancels on logout.
- Brand icon assets (`web/assets/icon.ico`, `web/assets/icon.png`, and `web/index.html` favicon SVG) remain visually consistent across desktop taskbar, window chrome, system tray, packaged executable, and browser tab.
- Offline startup with cached accounts enters read-only library browsing without getting trapped behind QR login.
- **Round 2 Responsive Proportions, 5-Ratio Immersive Stage, Living Acoustic Orb, Unified Account Hub & Zero-Scroll Pruner Deck**:
  - **Wide-Screen Proportions & 3-Axis Alignment**: Left rail scales smoothly (`216px` `<1440px`, `244px` `>=1440px`, `260px` `>=1920px`, slide-over drawer `<=1024px`) and houses a quiet collapsible「我的歌单」directory (`#rail-playlists-section`) filling tall vertical displays above `.rail-status-bar`. `.app-header-inner` shares the exact `max-width: min(1960px, 100%)` and horizontal padding axis as `.main-viewport` (`<= 2px` left-edge alignment across `2560×1440`..`960×1080`). Wide `每日推荐` (`>=1680px`) flows top-to-bottom column-major (`01..15` left, `16..30` right). Bottom player compact height is `76px` (`84px` comfortable) with wide-display track metadata (`clamp(240px, 24vw, 380px)` / `420px` at `>=1920px`).
  - **5-Ratio Fullscreen Immersive Stage (`stage.css` / `stage.js`)**: Strict single-line zero-wrap topbar (`<=48px`) across `2560×1440`, `2560×1080`, `1440×900`, `1024×768`, and `960×1080`, moving secondary actions (`切换音源`, `相似歌曲`, `复制当前句`, `复制全文`) into `#stage-more-popover`. Lyrics top-align cleanly with the cover and stay 100% unmasked at `scrollTop === 0` (top fade engages only on `.has-top-scroll`). Portrait/narrow/split-screen viewports (`max-aspect-ratio: 115/100` or `max-width: 1024px`) stack cleanly into a compact horizontal header card (`80px` cover + meta + transport) so the lyrics viewport strictly occupies `>= 55%` (typically `70–79%`) of viewport height.
  - **`0%–100%` Smart Radio + Living Acoustic Orb (`discover.js` / `discover.css`)**: Continuous `0–100` exploration slider with `0%`/`50%`/`100%` detents and semantic range labels (`0% · 纯熟歌回味` through `100% · 纯新歌探索`), paired with a bounded `144×144` 2D Canvas Living Acoustic Orb (`#discover-acoustic-orb`) whose warm-hearth -> sage -> sky-indigo fluid aura and harmonic wave rings morph smoothly in real time while strictly pausing rAF in `quiet_eco`, `gpu_performance_mode === 'eco'`, `game_mode`, `prefers-reduced-motion`, blur/hidden, inactive tab, or off-screen states.
  - **Redesigned `.account-hub` & Zero-Friction Multi-Channel Auth (`auth.js` / `cloud.js`)**: Opaque warm-paper grouped hub with **exactly one** `#hub-probe-all-btn` (`一键探测全部` -> `POST /api/channels/test-all`), live latency & status dots, inline `扫码登录` (NetEase & Bilibili native web QR modal `#bilibili-qr-modal`), inline `快捷登录` (isolated desktop WebView2 popup for QQ/酷狗概念版/咪咕/酷我), real-time SSE `event: channels` updates, and collapsible manual credentials disclosure.
  - **Single-Screen Zero-Scroll Pruner Deck Cockpit (`deck.js` / `deck.css` / `utils.js`)**: Locks `#view-deck.active` cleanly between header and bottom player bar with internal lyrics scrolling and a 3-zone tactile decision dock (coral-red prune left, audition/utility cluster + mode toggle center, sage-green keep right). Defaults to deterministic session-seeded `'random'` order prioritizing valid hydrated tracks before stubs, adds `[换一首]`, auto-hydrates stub tracks via `POST /api/songs/{id}/hydrate` (falling back to a crisp「曲目已下架 / 仅存 ID」notice), and replaces the legacy dark-slab cover fallback with a warm-paper minimalist vinyl disc SVG.
- Zero emojis, zero glassmorphism, zero translucent scroll bleed-through (`VAC-01`..`VAC-08` enforced by `tests/frontend/scan_ui.js` and `tests/frontend/visual_hygiene.test.js`), zero generic blue accent chrome, zero forbidden English top-level navigation labels, and zero untrusted `innerHTML` usage remain in `web/**`.
