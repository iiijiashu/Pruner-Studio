# Acceptance matrix

Completion claims must stop at the strongest level actually observed. Source, mocked tests, or packaging never substitute for real login, audible playback, resource measurements, or account mutation evidence.

## Verification levels

| Level | Required check | Expected evidence | Cleanup |
| --- | --- | --- | --- |
| Source / config | Repository contains no private runtime data; single-worker `Starlette + Uvicorn` binds `127.0.0.1`; exact Host/Origin/Fetch-Metadata checks, static allowlist (including `web/desktop_lyrics.html`) with `Cache-Control: no-store, must-revalidate` & `Pragma: no-cache`, trusted HTTPS upstream allowlists, `validate_custom_external_api` SSRF guard, `/api/cover` `*.126.net` SSRF guard, and zero `.src = ''` in `web/js/player.js` enforced | automated tests, static UI scan (`scan_ui.js`), `node --check`, static grep, `git diff --check`, staged-file audit | none |
| P0 safety, ghost-audio & credential isolation | Account-partitioned SQLite WAL isolation (`play_history`, `explicit_feedback`, `local_files`); atomic `poll_qr` verification; Windows DPAPI encryption & fail-closed quarantine for both `SessionStore` and `ChannelCredentialVault`; zero raw secrets in `public_summary()` / `/api/channels`; strict per-channel credential isolation (NetEase cookies never touch external channels; KuGou/Migu/QQ/Bilibili secrets scoped strictly to their own HTTPS hosts or validated `api_base`; cross-host redirect Cookie/header stripping); DTD/XXE-safe `amll_lyrics.py` XML parsing; `POST /api/desktop/command` strict action allowlist (`400` otherwise); `resetMediaElement` P0 ghost-audio prevention + `timeupdate` dual-playback suppression; Deck `{ preserveQueue: true, origin: 'deck' }` queue isolation; SSE exponential-backoff reconnect with logout cancel; truncated `trackIds` fail-closed; unknown-write freeze without premature resolution; frozen confirmation manifest + `session_generation` guard; bounded mini-batch unlike with circuit-breaker; `e.repeat` / IME keyboard guard & `L` relisten-later persistence; offline cached-account read-only entry | deterministic mocked backend & frontend tests plus code audit & CDP audio regression check | temporary test directories removed by tests |
| Audio, multi-channel search & discovery | Pure-Python EAPI signing; `privileges` merge; VIP/SVIP quality ladder (`jymaster -> hires -> lossless -> exhigh -> standard`, experimental `sky`) with single compatibility fallback & honest verified badges; Best-Quality Cross-Channel Arbitrator (`best_quality_race` vs `netease_first`, zero extra latency when NetEase is already verified lossless/Hi-Res/master); multi-channel providers (`kugou_lite`, `migu`, `qq`, `kuwo`, `bilibili`, `gdstudio`, `custom`), Cloud Disk, local lossless folder & `/api/stream/{token}` HTTP Range `200/206/416` + bounded LRU `AudioDiskCache` (audio MIME only, never caching HTML/errors); unthrottled paginated multi-channel search (`GET /api/search/multi` `limit` `1..100`, `offset` `0..1000`, `has_more`, dedicated provider `search_page()` without strict `TrackQuery` dropping, `±3s` title+artist dedupe, shared `_SEARCH_EXECUTOR`) & direct external playback; Martin Fiedler balanced dithered shuffle (`shuffle_engine.py`) + recency cooldown + `playHistoryStack`; 4-way local-first Smart Radio (`真·心动电台`), `沉睡宝藏`, `本地听歌洞察`, and 4 native read-only feeds with independent degradation & explicit-only feedback; safe same-origin Web Audio graph skipping direct cross-origin URLs | deterministic mocked `test_audio_and_protocol.py` + `test_backend.py` + zero-cross-leakage audit + live stream probe receipts | stop playback and remove temp files |
| Build / package | Windows GUI one-file executable (`0.4.0`, `console=False`, branded via `web/assets/icon.ico`, bundling `web/` including `web/desktop_lyrics.html` plus `webview`/`pythonnet`/`clr_loader` DLLs via `collect_all`) built from documented command with `requirements-build.lock` hashes, Start Menu shortcut script (`scripts/install-start-menu.ps1`), and CLI/headless env overrides preserved | executable path, size, timestamp, SHA-256, PKG asset audit, Windows version resource (`0.4.0`) & `.lnk` check | retain release artifact |
| Local runtime | Executable or server starts one owned process under data-dir lock (relaunch wakes existing window cleanly), serves `/api/health` (`"0.4.0"`), `/api/status`, `/api/settings` (`appearance`, `desktop_lyrics_style`, `desktop_lyrics_bounds`), `/api/catalog`, `/api/channels`, `/api/search/multi?q=test&offset=0&limit=100`, `/api/audio/candidates`, `/api/recommend/smart-radio`, `/api/recommend/dusty-gems`, `/api/insights`, `/api/queue/smart-shuffle`, `/api/cache/audio/stats`, `/api/desktop/command`, `/desktop_lyrics.html`, rejects invalid Host (`403`), invalid desktop command (`400`) & non-126.net `/api/cover` (`400/403`), launches WebView2 with brand icon/tray/transparent floating lyrics/global hotkeys (or browser/headless fallback), persists SQLite WAL & DPAPI stores, and exits cleanly | process path/command line, health/security responses, runtime-data path | stop only the test process |
| Desktop UI, themes & responsive | 4-destination collapsible rail (`发现` / `音乐库` / `歌单清理` / `设置与同步`), dedicated full-page `#view-search` (100/page + `加载更多`), equal 2-col `发现` hero (`真·心动电台` continuous `1%` slider + `私人漫游`) + 12-card `沉睡宝藏` + `本地听歌洞察`, 3-tab `设置与同步` (`偏好设置` w/ `外观` card / `多渠道与会员` w/ collapsed `高级凭据` / `同步与备份`), seven themes plus automatic system selection + 6 accents + density + font scale without FOUC or overriding Keep/Prune, narrow-width drawer collapse (`1024×768` including 125%/150% DPI), persistent bottom player (`词` + inline lyric + `更多` overflow), unified right-click context menu, online-first `Ctrl+K` palette, Deck own-song non-overlapping static lyrics + segmented toggle, toast cap/dedupe/quiet mode, immersive YRC stage (`160×160` fluid canvas, `38%/62%` desktop grid, `− \| 0.0s \| +` offset stepper, `收起` / `Esc` close + mid-song sync + rAF/canvas unmount), `/desktop_lyrics.html` transparent karaoke word-sweep preview & hover style popover, focus outlines, zero console errors, zero HTTP 500s, zero horizontal overflow | real browser / WebView2 screenshots at `1440×900` and `1024×768` plus CDP regression & interaction notes | close only the test tab/window |
| Resource & performance budgets | Measure process-group memory, frame time, and track-switch latency under target desktop workload (see concrete budgets below) | measured working-set snapshots, frame-time telemetry, switch-latency p95 receipts | stop measurement workload |
| External read | QR login identifies the real account and fetches real liked, self-created, subscribed, cloud, lyrics/chorus, and recommendation feeds | user-visible account and playlist list without logging credentials | logout only when test calls for it |
| Live audio | Original VIP/SVIP track is audible with accurate badge; Cloud Disk / local lossless / qualifying multi-channel & direct cross-channel tracks are audible and visibly labelled; Web Audio loudness/crossfade never mutes external streams; rapid switch & Space pause emit zero ghost audio; bad matches are rejected | observed playback plus non-sensitive diagnostics | stop playback |
| External write | Create a temporary private playlist, add known tracks, and verify upstream result | resulting playlist visible in the official account | requires explicit user approval before test; remove only the test playlist if approved |
| Unlike / remove | Selected song is actually removed from liked or owned playlist, failures/unknowns remain locally, and reconciliation stays read-only | official-account observation and application result | requires the user to nominate a disposable target and authorize the test |

## Resource and performance budgets (GPT-6.2 Sol arbitrated)

Release sign-off for the upgraded desktop build requires measuring the full process group (host executable + embedded Uvicorn backend + Edge WebView2 child processes) against these hard ceilings:

| Metric | Target budget | Measurement scope & notes |
| --- | --- | --- |
| Process group private working set — Startup | `<= 220 MiB` | Cold start to idle interactive shell |
| Process group private working set — Steady playback | `<= 260 MiB` | Active audio playback with YRC lyrics and bottom player |
| Process group private working set — 10k-track peak | `<= 320 MiB` | Browsing/filtering/paginating a 10,000-track SQLite catalog |
| Process group private working set — 1h / 300-switch recovery | `<= baseline + 30 MiB` | After 1 hour or 300 track switches (including opening/closing immersive stage); confirms `160×160` fluid canvas unmount & rAF cancel, bounded LRU memory/disk caches, discrete cover sizing, and single active `<audio>` transport |
| UI frame time (60 Hz) p95 | `<= 16.7 ms` | Scrolling catalog/search, active YRC word highlight, and 24fps `160×160` fluid canvas in `柔和动态` mode |
| Hot track switch latency p95 | `<= 0.8 s` | Switch to a cached/prefetched track URL, LRU audio cache hit, or local lossless file |
| Cold track switch latency p95 | `<= 2.0 s` | Switch requiring fresh upstream URL/lyrics resolution |
| Sync-concurrent playback overhead | `<= 200 ms` | Additional playback start/control latency while a background playlist sync is running (verifies `_auth_lock` decoupling and staged SQLite replacement) |

## Explicit P0 safety, multi-channel & Wave 4 acceptance gates

Every release candidate must verify these concrete regression gates before acceptance:

1. **P0 ghost-audio & Deck isolation safety**:
   - Zero `.src = ''` or `.src = ""` assignments in `web/js/player.js`; all resets go through `resetMediaElement(el)` (`pause` + `removeAttribute('src')` + `load`).
   - Rapid consecutive `playSong()` switches trigger at most ONE active non-paused media element at any time, exactly 1 `getAudio` call per switch, and zero stray `handleAudioElementError` retries; `timeupdate` suppresses accidental dual playback.
   - Deck audition playback uses `{ preserveQueue: true, origin: 'deck' }` so it never overwrites or advances the global listening queue and displays non-overlapping own-song static lyrics (`未在播放 · 按 P 试听`) when mismatched.
2. **Delayed & unknown write safety**:
   - Soft-commit 3-second countdown remains cancellable via `Z` until dispatch; closing the app cancels uncommitted soft commits.
   - Unknown transport outcomes transition to `sent_unknown`, persist across restart, block blind retries, and NEVER auto-resolve on transient errors or partial/truncated playlist reads.
   - Batch confirmation freezes the exact manifest (`manifest_hash` + `session_generation`); switching accounts invalidates pending manifests.
   - Unlike executes in bounded mini-batches with one verification read per mini-batch and circuit-breaks on the first unknown error or rate limit.
3. **Truncated playlist fail-closed**:
   - Authoritative sync and write prechecks fail closed if `trackIds` is absent or count-mismatched against `trackCount`; never silently fall back to partial `tracks` arrays.
4. **Cross-account & per-channel credential isolation**:
   - SQLite tables partition playlists, tracks, `play_history`, and `explicit_feedback` by `account_id`; switching accounts never leaks tracks or lets Account B mutate Account A caches.
   - `poll_qr` verifies `account()` using the candidate cookie before replacing the active session.
   - Windows persistence encrypts NetEase cookies (`SessionStore`) and per-channel credentials (`ChannelCredentialVault`) via DPAPI (`CRYPTPROTECT_UI_FORBIDDEN`) while quarantining/failing closed on corrupt files without overwriting unverified bytes.
   - `public_summary()` and `GET /api/channels` NEVER return raw `cookie`/`token`/`sessdata`/`dfid`; every channel `api_base` passes `validate_custom_external_api` SSRF checks.
   - Cross-host HTTP redirects strictly strip credentials/`Cookie` headers and reject untrusted/non-HTTPS upstream hosts.
   - `external_sources.py` (both audio resolution and paginated `search_page()`) and `/api/stream/{token}` never send NetEase cookies to external providers; KuGou/Migu/QQ/Bilibili credentials go strictly to their own trusted HTTPS hosts or validated `api_base`; `gdstudio` receives zero credentials; `custom` receives zero NetEase credentials.
5. **Audio cache, Web Audio, AMLL TTML & Desktop bridge safety**:
   - `AudioDiskCache` writes atomically, enforces `audio_cache_limit_mb` LRU eviction (`0` disables), and caches only verified `200` audio MIME streams (never HTML/error responses).
   - Web Audio loudness normalization / crossfade / analyser graph attaches ONLY to same-origin `/api/stream/{token}` URLs and skips direct cross-origin URLs so playback never silently mutes.
   - `amll_lyrics.py` fetches zero-Cookie over HTTPS with bounded timeout (`<=3s`), bounded LRU (100 entries), and DTD/XXE-disabled XML parsing.
   - `POST /api/desktop/command` strictly allowlists `next|prev|toggle_play|toggle_like` (rejecting all other actions with `400`) under full CSRF/Origin/Host/Fetch-Metadata/JSON checks; `cloud.js` SSE reconnects with exponential backoff and cancels on logout.
6. **Keyboard repeat, IME, global hotkeys & relisten-later safety**:
   - Deck destructive/keep/skip/relisten shortcuts work exclusively on `歌单清理` and ignore `e.repeat` and `e.isComposing`.
   - Global `Space` play/pause respects `歌单清理`, modal, palette, input/button focus, `e.repeat`, IME, and modifier guards.
   - `L` persists `稍后再听` state across reloads and integrates with the deck filter.
   - Windows global hotkeys (`Ctrl+Alt+Right/Left/P/L/D`) cleanly unregister via `UnregisterHotKey` + `WM_QUIT` on disable or exit; Desktop Floating Lyrics (`web/desktop_lyrics.html`) uses clean SVG controls and zero emojis.
7. **Offline cached-library entry**:
   - Launching while unauthenticated or offline with `cached_accounts` present enables direct read-only access to the local SQLite library without being blocked by the QR login wall.

## v0.4.0 验收范围

| 功能 | 验证入口 | 证据边界 |
| --- | --- | --- |
| 多账号、云盘分页、切换时的同步、渠道状态、更新检查 | 后端 Phase 4 回归测试 | 临时数据库、Windows DPAPI、模拟上游 |
| 账号中心、搜索游标、更新反馈 | 前端 Phase 4 测试、UI 扫描 | 合成账号与响应 |
| 写入与缓存保护 | Phase 5 HTTP、SQLite 与前端回归 | 空喜欢列表核对、跨账号旧请求、大快照导入与轮换、风控、云盘短页、DNS 固定、缓存格式、进度与播放时序 |
| Windows 便携程序 | `scripts/verify-windows.py` | PE 版本与 DPI 清单、UPX 节、嵌入资源、隔离端口实际请求与正常退出 |
| 发布流程 | Windows CI 和标签发布工作流 | GitHub 执行记录与 Release 附件由实际发布产生 |
| 桌面安装、真实会员身份和写入权限 | 安装程序、WebView2 窗口、实际账号授权 | 需要对应环境的独立验收 |

### 2026-11-25 Pruner Studio 0.4.0 本地 Stage 5 验收

- 后端全量测试以 `src` 和 `tests/backend` 加入 `PYTHONPATH` 运行 `pytest -q tests/backend`。结果为 216 项通过，另有 236 项子测试通过和 1 条 Starlette `TestClient` 弃用告警。前端 `node tests/frontend/run_all.js` 的 23 组测试通过。UI 扫描、24 个 JS 文件的语法检查及 `python -m compileall -q src` 通过。
- 此前 Stage 2.5 的独立运行记录覆盖了 Win32 桌面歌词隐藏与重开、WinForms 全屏 `RestoreBounds` 恢复，以及 Headless Edge 下 48 组主题与强调色组合的对比度和 200 字符历史记录的布局。
- Windows x64 便携包 `dist/Pruner-Studio-latest.exe` 为 27,106,617 字节，SHA-256 为 `297bc68308c8c505096b911b32859a0dfb7e3f9f5a6bfe411a8baefbb5c1f91e`。以 `PRUNER_VERIFY_EXE` 指向该包运行 `scripts/verify-windows.py`，核对了 0.4.0 版本、PerMonitorV2 清单、启动器和 139 个内嵌 PE 的 UPX 节、41 个 Web 文件以及 23 个应用模块和启动脚本。隔离实例通过状态、账号、设置、推荐和 LAN 状态请求，并通过首次 TLS 证书生成、配对链接指纹及握手校验，退出码为 0。
- 冻结 Windows 包的双设备 TLS 实测使用 33,554,476 字节的合成 WAV。撤销设备 A 后，其响应在累计收到 851,968 字节时终止，旧令牌重新请求返回 401。设备 B 完整读完同一文件，测试后文件句柄已释放。后端用例也覆盖撤销发生在响应登记时返回完整 401。
- Android 调试签名包 `dist/Pruner-Studio-Mobile.apk` 为 24,121,772 字节，SHA-256 为 `71d74f8df10706e36c853bfba458983ecc8b41dcb680c458328bca9fda0ed87f`。它与 Gradle 输出逐字节一致，`apksigner` 核验 v2 签名通过。包名为 `com.prunerstudio.mobile`，最低 API 26，目标 API 35。清单关闭备份与明文流量，前台播放服务未导出。
- `gradlew.bat :app:testDebugUnitTest :app:assembleDebug --no-daemon --offline` 通过，46 项 JVM、Robolectric 和 Compose 测试全部通过。回归测试确认解绑、SSE 撤销和替换配对均能唤醒处于阻塞读取中的 Media3，401 在错误体未结束或调用方取消时仍能清除旧配对，曲库 GET 能从断连恢复，POST 只尝试一次。
- API 36 模拟器安装了同一 SHA-256 的 APK，经 ADB 回环和冻结 Windows 包联调。配对深链先展示桌面证书指纹，经手动确认后完成 TLS 配对并连上 SSE。手机能浏览桌面曲库和播放合成曲目，Windows 撤销后手机界面清除了配对。该环境的播放证据来自系统媒体会话。
- Astra High 只读复审分别核对了桌面授权登记与断流、Android 撤销和 401 处理，以及只读请求的连接恢复范围。原有阻断项已修复。
- Windows 旧版运行时可保留使用。切换到新版时先从托盘退出旧版，再打开 `Pruner-Studio-latest.exe`，避免单实例机制唤醒旧窗口。实体手机 Wi-Fi 扫码、锁屏与蓝牙播放、真实网易云登录与线上写入仍需对应设备和账号环境。安装程序、GitHub 发布流程和 WebView2 长时性能留作各自的独立验收。

### 2026-11-24 Windows x64 本地结果（桌面歌词彻底重构与主界面质感升级，已替代）

- 后端 `194` 项单元测试全部通过（含 `test_desktop_lyrics_overhaul.py` 新增 `7` 项，覆盖动态最小高度计算、陈旧 `772×64` 自愈、陈旧 `_on_closed` 句柄守卫、`WS_EX_NOACTIVATE`/`SWP_NOACTIVATE`、`GetAsyncKeyState` + 8 方向边缘缩放、`/resize-start` `/fit` `/center` 接口，以及隐藏 WinForms 窗体 10 轮校准 `Size`/`ClientSize` 零漂移 `820×120` 且不抢占焦点验证）。前端 `20` 组测试（`node tests/frontend/run_tests.js`，含 `phase9_lyrics_and_tactile.test.js`）、UI 扫描（`node scripts/scan_ui.js`）、`web/js/*.js` 全量 `node --check` 语法检查及 Python `compileall -q src` 均通过。
- 最终 `dist/Pruner-Studio.exe` 为 24,007,614 字节，SHA-256 为 `5ccf2fff7a611583874e5fb3152b88625d16150f4b83064bc9a74147ceb19618`。两个版本字符串均为 0.4.0，嵌入清单包含 PerMonitorV2，启动器与 96 个内嵌 PE 均无 UPX 节，40 个打包 Web 文件与工作区逐字节一致。21 个内嵌应用模块及启动脚本的代码对象与当前源码编译结果一致。
- 使用临时数据目录与随机端口 60025 启动该文件（`scripts/verify-windows.py`）。`/api/status`、`/api/accounts`、`/api/settings` 和 `/api/recommend/daily` 均返回 HTTP 200，测试进程正常退出，退出码 0。

### 2026-11-22 Windows x64 本地结果（前一构建记录，已替代）

- 后端 `187` 项单元测试全部通过（含 `test_phase7f_and_phase8.py` 新增 `6` 项）。前端 `19` 组测试（`node tests/frontend/run_tests.js`）、UI 扫描（`node scripts/scan_ui.js`）、`web/js/*.js` 全量 `node --check` 语法检查及 Python `compileall -q src` 均通过。
- 前一构建产物 `dist/Pruner-Studio.exe` 为 23,977,167 字节，SHA-256 为 `249bd880b5512a13fb020f2fcf9a1a78583cb83b78c7d2c087f94d865416aa9c`。两个版本字符串均为 0.4.0，嵌入清单包含 PerMonitorV2，启动器与 96 个内嵌 PE 均无 UPX 节，40 个打包 Web 文件与工作区逐字节一致。21 个内嵌应用模块及启动脚本的代码对象与当前源码编译结果一致。
- 使用临时数据目录与随机端口 64697 启动该文件（`scripts/verify-windows.py`）。`/api/status`、`/api/accounts`、`/api/settings` 和 `/api/recommend/daily` 均返回 HTTP 200，测试进程正常退出，退出码 0。
- 上一轮打包验收按发布工作流的命令组装并解压便携 ZIP，验证三项文件清单与 EXE 哈希一致，独立临时用户目录下的开始菜单快捷方式指向解压后的 EXE 并使用其嵌入图标。
- 两份工作流 YAML 已解析。GitHub 托管 CI、Release 发布、Inno Setup 编译与真实账号联机验收尚待相应环境执行。

### 2026-11-22 Windows x64 本地结果（更早构建记录，已替代）

- 后端 `145` 项通过，其中 Phase 5 新增 14 项。已有回归还覆盖 SSE 重连、账号进度和快照校验。前端 `16` 组通过，UI 扫描、23 个 JS 文件语法检查及 Python compileall 通过。
- 更早构建产物 `dist/Pruner-Studio.exe` 为 23,917,065 字节，SHA-256 为 `84ff41134d1d7ef8c1357ec17f4f7891680ed02a72a6ca036433695d98171cbf`。两个版本字符串均为 0.4.0，嵌入清单包含 PerMonitorV2，启动器与 96 个内嵌 PE 均无 UPX 节，40 个打包 Web 文件与工作区逐字节一致。21 个内嵌应用模块及启动脚本的代码对象与当前源码编译结果一致。
- 使用临时数据目录与随机端口 54967 启动该文件。`/api/status` 和 `/api/accounts` 均返回 HTTP 200，测试进程正常退出，退出码 0。

## Historical upgrade acceptance status (`0.3.0` / Wave 4)

- **Status**: Version bumped to `0.3.0` with Wave 4 polish, theme engine, online-first paginated multi-channel search (`#view-search`), transparent karaoke desktop lyrics + `/api/desktop/command`, unified right-click context menu, player bar overflow & inline lyric, P0 ghost-audio fix, SSE reconnect, and Deck queue/lyrics fixes.
- **Required before release sign-off**:
  - Full backend (`tests/backend/test_backend.py` + `tests/backend/test_audio_and_protocol.py`) and frontend (`run_tests.js` incl. `wave4_experience.test.js` + `scan_ui.js` + `node --check`) suites green.
  - Headless Chrome CDP P0 ghost-audio regression test & `1440×900` / `1024×768` visual verification across themes, `#view-search`, Deck, Player overflow/inline lyric, Stage, and `/desktop_lyrics.html` green with zero console errors, zero HTTP 500s, and zero horizontal overflow.
  - Updated `requirements-build.lock` (hash-locked Windows x64 / CPython 3.11 wheels for the `0.3.0` dependency set), freshly rebuilt `dist\Pruner-Studio.exe` (`0.3.0`, `console=False`, branded via `web/assets/icon.ico`), and frozen EXE headless loopback smoke verified.
  - Interactive desktop & live-account gates (interactive WebView2 GUI/transparency/icon/tray/relaunch wake, audible VIP/multi-channel playback, memory/frame budgets, and user-authorized external writes/unlike) remain unaccepted until explicitly observed with user participation.

## Historical pre-release baseline (v0.2.0, 2026-08-30 — superseded)

> Retained for audit history only; superseded and invalidated by subsequent `0.3.0` source, UI, dependency, lockfile, and spec changes.

- Source/tests: 45 backend tests and 9 frontend suites passed; Python compilation, `web/js/*.js` syntax checks, UI pattern scan, and `git diff --check` passed.
- Package: `dist\Pruner-Studio.exe` (`0.2.0`, 16,917,671 bytes, SHA-256 `0D653E0B364CFE38A6103BFFF8C683FD656015D5E4069854280DCA44041F962E`), verified on isolated loopback runtime (`127.0.0.1:18991`), single-instance lock enforcement, unauthenticated QR render at `1440×900` / `1024×768` / `768×900`, and CI read-only SHA-pinned workflows.
