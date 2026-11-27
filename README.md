# Pruner Studio

Pruner Studio (`0.4.0`) 是一个本机运行的网易云音乐日常桌面客户端、多渠道高解析音源聚合中心与安全歌单清理工具。扫码登录自己的账号后，你可以在支持多主题与外观定制的独立桌面窗口中享受多渠道最高音质择优播放、在线优先全页分页搜索、本地优先智能电台、AMLL 流体歌词舞台与透明逐字桌面悬浮歌词，同时使用专为大规模曲库设计的歌单清理视图安全、可控地整理歌单。

## 核心功能

- **四目的地侧栏 + 全页搜索视图**（宽屏可折叠，窄屏如 `1024×768` 及 125%/150% DPI 缩放下自动收起为抽屉）：
  - **发现**：等宽双栏听歌入口——**「真·心动电台」**（4 路本地优先混合电台：沉睡宝藏 + 近期常听 + 多红心共同相似图交集 + FM/日推探索，配备连续 `1%` 步进 `20%–80%`「熟歌 ↔ 探索」滑块）与**「私人漫游」**；下方展示紧凑**「本地听歌洞察」**统计栏、最多 12 首横向**「沉睡宝藏」**卡片条、每日推荐（30 首）、最近播放与相似歌曲。本地 `play_history` 仅记录真实收听（忽略快切与歌单清理操作），重排仅采纳显式反馈。
  - **音乐库**：统一浏览「我喜欢的音乐」、自建歌单、收藏歌单（只读播放）、网易云云盘、本地无损目录以及 JSON/CSV 离线导入；支持表格/五列卡片双视图、搜索筛选排序、50 首分页与规则抽屉（规则仅预览与暂存，绝不绕过确认直接修改云端）。
  - **歌单清理**：专注单曲试听与决策；试听采用队列保护模式（不覆盖也不推进全局听歌队列），当前卡片与全局播放曲目不同时自动加载并展示本曲静态歌词（「未在播放 · 按 P 试听」）；配备「立即同步 3s | 批量确认」分段切换（默认 3 秒本地软提交，`Z` 撤销尚未发出的云端写入），支持 `L` 标记持久化「稍后再听」，内置防键盘连发（`e.repeat`）与输入法合成期（IME）误触保护。
  - **设置与同步**（顶部三标签切换）：
    - **偏好设置**：顶部内置**「外观」**主题卡片（`深色` / `午夜` / `浅色` / `跟随系统` 四主题、6 款预设强调色 + 自定义取色器 + 封面动态取色、舒适/紧凑密度、90%–125% 字体缩放，启动零闪烁应用且始终保持绿色保留/红色移除语义不变），以及首选音质阶梯、响度均衡、交叉淡入淡出（默认 `150ms`）、歌词景深虚化、低频呼吸光、AMLL TTML 逐字歌词回退、透明桌面悬浮歌词开关与穿透锁定、Windows 全局热键开关、有界 LRU 本地音频磁盘缓存、本地无损音乐目录扫描、视觉模式（`柔和动态` / `静谧省电`）与清除本地反馈。
    - **多渠道与会员**：集中管理 `网易云`、`酷狗概念版`、`咪咕音乐`、`QQ音乐`、`酷我`、`B站`、`GDStudio`、`自定义` 等渠道卡片；卡片默认展示单行连接/VIP/加密状态与主操作「测试连通性」，原始凭证字段收折在默认关闭的「高级凭据」面板内，支持 Windows DPAPI 加密保存/清除（绝不回显明文）、自定义 `api_base`（含 SSRF 校验）以及「全渠道最高音质择优」vs「网易云原曲优先」仲裁模式切换。
    - **同步与备份**：管理歌单同步实时进度（SSE 断线指数退避自动重连）、待执行分组队列、未知结果只读核对恢复、自动快照备份、本地导入与最近 100 条操作记录。
  - **在线优先 `Ctrl+K` 快速搜索与全页搜索结果页（`#view-search`）**：`Ctrl+K` 默认进入「全渠道聚合」（记住上次所选渠道），输入关键词后上方先展示最多 5 条「曲库中」本地命中，下方实时展示多渠道在线结果；按空选 `Enter` 或 `Shift+Enter`（或点击「查看全部结果」）进入全页搜索视图，支持每页 **100 条**分页加载（`offset` / `has_more` /「加载更多」）、相关度/音质优先排序、渠道徽标与外部曲目直接播放。
  - **统一右键菜单**：在音乐库、发现、播放队列与搜索结果行上右键即可唤出上下文菜单（播放、下一首播放、加入歌单、喜欢/取消喜欢、发现页「不再推荐」、查看相似、复制歌名-歌手），支持键盘导航与视口防溢出定位。
- **全局持久底部播放器、安全音频引擎与 LRU 磁盘缓存**：
  - 底部播放栏提供直达**「词」**桌面悬浮歌词开关、单行实时歌词显示以及**「更多」**收纳弹窗（收纳快退/快进 5 秒、副歌跳转、相似歌曲、切换音源版本、侧边歌词与沉浸舞台入口），保持低噪音与窄屏零溢出；提示 Toast 限制最多同时显示 3 条、2 秒自动去重，并为音量/偏移/切模式提供轻提示 `quiet` 模式。
  - 内置 `resetMediaElement` 媒体重置与双 `<audio>` 并发播放拦截保护，彻底杜绝快切/暂停时的幽灵重叠音轨。
  - 通过纯 Python EAPI 协议请求网易云 VIP/SVIP 最高兼容音质阶梯（`jymaster` 超清母带 → `hires` Hi-Res → `lossless` 无损 FLAC → `exhigh` 极高 → `standard` 标准，含 `sky` 沉浸声实验），界面只展示经后端核验的真实格式、码率与采样率；跨渠道音质仲裁器在网易云原曲为有损或受限时自动并发择优升级。
  - 采用 **Martin Fiedler 均衡抖动随机算法** + 近期播放冷却 + 艺人/专辑防连播与精确 `playHistoryStack` 上一曲回溯，一轮不重复。
  - 对同源 `/api/stream/{token}` 音频流启用安全的 Web Audio 链路（响度均衡 + 150ms 交叉淡入淡出 + 低频节奏呼吸光），对直接跨域外部流自动旁路避免静音；经校验的 `200` 音频流可按上限写入有界 LRU 本地磁盘缓存（`AudioDiskCache`，默认 `512 MiB`，`0` 关闭）。
- **AMLL 流体歌词舞台、透明卡拉 OK 桌面悬浮歌词与 Windows 全局热键**：
  - 点击底部封面按需展开全屏沉浸舞台：宽屏（`>=1200px`）采用 `38% / 62%` 左封面右歌词布局，基于 `64×64` 封面四象限取色的 `256×256` 2D Canvas 流体油画网格背景（关闭即卸载并取消 `rAF`，失焦/隐藏/暂停/省电自动暂停）、封面呼吸动效、距离梯度透明度歌词、可选景深虚化、大于 5 秒间奏三点呼吸提示、右上角 `− | 0.0s | +` 单曲歌词偏移微调（点击中间归零），以及零 Cookie `amll-ttml-db` 逐字歌词回退；支持曲中打开即刻定位当前行，点击左上角「收起」或按 `Esc` 返回。
  - **透明卡拉 OK 桌面悬浮歌词**（`web/desktop_lyrics.html`）：透明无边框置顶可调大小窗口（默认 `820×120` 底部居中，自动记忆窗口位置与尺寸 `desktop_lyrics_bounds`，支持不影响透明度的鼠标穿透锁定），双层描边阴影保证任意壁纸下清晰可读，支持 YRC 逐字卡拉 OK 扫色、单/双行切换、翻译开关、悬停迷你播控（上一首/播放暂停/下一首 via `/api/desktop/command`）与悬停样式设置弹窗（字号 `18..48`、已播/未播配色、描边、底色透明度、对齐方式，持久化并实时 SSE 同步）。
  - **快捷键与 Windows 全局热键**：应用内支持 `Space` 播放/暂停（歌单清理外）、`Ctrl+K` 全局搜索、`Ctrl+←/→` 上/下一首、`Ctrl+↑/↓` 音量 ±5%、`?` 快捷键帮助面板；Windows 全局热键（可在偏好设置中开关）支持 `Ctrl+Alt+Right` 下一首、`Ctrl+Alt+Left` 上一首、`Ctrl+Alt+P` 播放/暂停、`Ctrl+Alt+L` 喜欢/取消喜欢、`Ctrl+Alt+D` 开关桌面悬浮歌词。
- **安全与离线保障**：
  - 账号隔离的 SQLite WAL 本地曲库与听歌历史缓存；离线或未登录时，如本机存在已缓存账号曲库，可直接只读进入音乐库而不被二维码页面阻断。
  - 只有网易云明确确认成功的歌曲才会从本地移除；批量确认在弹窗打开时冻结清单快照与会话代次；网络结果不确定时落盘恢复记录并冻结写入，重启后仅允许完整快照只读核对或显式忽略，绝不盲目重试。

## 使用 Windows 单文件桌面版

**Windows 一分钟安装**。在 [GitHub Releases](https://github.com/iiijiashu/Pruner-Studio/releases) 选择 Windows x64 发布附件。

- 安装版：运行 `Pruner-Studio-v0.4.0-win64-setup.exe`，按提示为当前用户安装，从开始菜单启动。可以在 Windows「已安装的应用」中卸载。
- 便携版：将 `Pruner-Studio-v0.4.0-win64-portable.zip` 完整解压到固定目录，双击 EXE。如需开始菜单入口，在该目录运行 `.\install-start-menu.ps1`。
- 右上角可扫码添加多个网易云账号、切换账号或进入免登录本地模式。平台矩阵区分凭据配置、连通探测和账号身份。云盘同步取完全部分页后才替换缓存。
- 「设置与同步 → 检查更新」读取公开发行信息，点击发行说明后可自行下载。可用 `Get-FileHash` 对照同一版本的 `SHA256SUMS.txt`。

从 Releases 下载或本地构建 `Pruner-Studio.exe`（`0.4.0`），双击直接启动独立的 **Edge WebView2 桌面窗口**（无黑框控制台窗口，内置品牌图标），支持最小化到 Windows 系统托盘（托盘菜单支持「显示窗口」、「播放 / 暂停」、「桌面歌词」、「解锁桌面歌词」与退出）、透明桌面悬浮歌词与全局热键。若系统未安装 WebView2 Runtime，程序会自动回退到默认浏览器打开本机地址（`http://127.0.0.1:8765/`，端口占用时自动顺延）。

同一数据目录一次只允许一个实例运行，以免会话、SQLite 曲库或恢复日志互相覆盖；当已有实例正在运行（包括最小化到托盘）时，再次启动程序会自动唤醒并前置已打开的主窗口。程序只监听 `127.0.0.1` 本机回环地址；登录会话、多渠道加密凭证、曲库与听歌历史数据库、LRU 音频缓存、设置、规则、恢复日志和快照备份均保存在当前 Windows 用户的 `%LOCALAPPDATA%\Pruner Studio`，不会写入程序目录。

如需在 Windows 开始菜单搜索 `Pruner` 直接启动，可运行注册脚本创建开始菜单快捷方式（可选添加 `-AlsoDesktop` 同时在桌面创建快捷方式，不会修改或删除已有桌面文件）：

```powershell
.\scripts\install-start-menu.ps1
```

可选启动环境变量（用于 CLI、调试或自定义启动方式）：

- `PRUNER_STUDIO_BROWSER=1`：跳过桌面窗口，直接在系统浏览器中打开界面。
- `PRUNER_STUDIO_NO_GUI=1`：以纯本地服务模式运行，不创建 WebView2 桌面窗口、悬浮歌词窗或系统托盘。
- `PRUNER_STUDIO_NO_BROWSER=1`：配合 `PRUNER_STUDIO_NO_GUI=1` 以无头本地服务模式启动（供脚本或自动化测试使用）。

首次使用：

1. 双击程序，用网易云音乐手机 App 扫描二维码登录（若已有本地缓存且处于离线状态，也可点击直接进入离线音乐库）。
2. 在「发现」中一键开启「真·心动电台」或「私人漫游」，自由拖动 `1%` 步进探索滑块并查看「沉睡宝藏」与「本地听歌洞察」；非输入/非弹窗/非清理视图下按 `Space` 可直接播放/暂停，按 `?` 查看全部快捷键；按 `Ctrl+K` 进行在线优先多渠道搜索或进入全页分页搜索；点击底部封面进入 AMLL 流体沉浸舞台，点击「词」呼出透明桌面悬浮歌词。
3. 进入「歌单清理」快速筛选歌单：按 `P` 试听当前卡片（不打断主队列），`C` / `↑` 跳至副歌，`←` / `A` 移除，`→` / `D` 保留，`L` 稍后再听，`Space` 跳过至本轮末尾，`Z` 撤销尚未提交的移除。
4. 在「设置与同步」中：
   - **偏好设置**：定制主题/强调色/密度/字号、调整音质、响度均衡、淡入淡出、歌词效果、桌面悬浮歌词、全局热键、本地无损目录与音频磁盘缓存上限。
   - **多渠道与会员**：启用/配置第三方渠道（原始凭据折叠在「高级凭据」中）、测试连通性并选择音质仲裁策略。
   - **同步与备份**：刷新歌单进度、处理待执行分组队列与查看备份/历史记录。

程序直接连接网易云音乐及所配置的渠道接口，不需要强制另行部署 `NeteaseCloudMusicApi` 等网关服务。

## 架构概览

- **后端与桌面壳层**：单进程 `Starlette + Uvicorn` 本机回环服务（静态资源 `no-store` 防 WebView2 缓存陈旧）+ `httpx` 连接池 + 账号隔离 `SQLite WAL` 数据库（含 `play_history`、`explicit_feedback`、`local_files`）+ Windows DPAPI 会话与多渠道凭证库（`SessionStore` / `ChannelCredentialVault`）+ 有界 LRU `AudioDiskCache` + `pycryptodome` 纯 Python EAPI/WEAPI 协议层 + `shuffle_engine` / `amll_lyrics` + `pywebview`（Edge WebView2 主窗与透明悬浮歌词窗）+ `pystray` 托盘 + 纯 `ctypes` Windows 全局热键。
- **前端界面**：原生 ES Modules 与零框架 DOM 渲染，零闪烁多主题引擎（`theme.js`），统一右键上下文菜单（`context_menu.js`），全页分页搜索（`search_view.js`），1.5px SVG 矢量图标体系，离散封面尺寸与四象限封面取色，`256×256` 2D Canvas 流体背景，固定行高零重排 YRC/LRC/TTML 歌词引擎，安全同源 Web Audio 音频链路，以及独立透明逐字卡拉 OK `web/desktop_lyrics.html` 悬浮歌词页。
- 详细设计与验收文档见 [ARCHITECTURE.md](ARCHITECTURE.md)、[DESIGN.md](DESIGN.md) 与 [ACCEPTANCE.md](ACCEPTANCE.md)。

## 从源码运行

需要 Python 3.11+；Node.js 仅用于运行前端测试与静态扫描。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m pruner_studio
```

当前项目不发布独立 wheel；源码方式以克隆完整仓库后运行上面的 editable 安装为准，因为界面资源保留在仓库的 `web/` 目录中。

## 测试与打包

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m unittest discover -s tests/backend -v
node tests/frontend/run_tests.js
node tests/frontend/scan_ui.js
.\scripts\build-windows.ps1
```

打包产物位于 `dist\Pruner-Studio.exe`。如已在本地 `.venv` 准备好当前依赖、仅需本地开发重打包，可跳过哈希锁安装步骤：

```powershell
.\scripts\build-windows.ps1 -SkipLockInstall
.\scripts\install-start-menu.ps1
```

Windows 单文件构建固定使用 64 位 CPython 3.11；`requirements-build.lock` 记录精确 wheel 版本与 SHA-256 校验值，`Pruner-Studio.spec`（含 `console=False`、品牌图标 `web/assets/icon.ico`、`collect_all` 打包 `webview`/`pythonnet`/`clr_loader` 及完整 `web/` 资源包含 `desktop_lyrics.html`）和统一版本源 `src/pruner_studio/version.py`（`0.4.0`）一并纳入仓库。每次更新 `pyproject.toml` 依赖后，发布打包前需先在 Windows x64 / CPython 3.11 环境下重新生成并核验 `requirements-build.lock`。源码运行支持 Python 3.11 及以上版本。

## 隐私与安全边界

- 这是单用户本机工具，只监听 `127.0.0.1` 且不开启 CORS，请勿将端口代理到局域网或公网。
- 仓库不包含任何账号 Cookie、渠道凭证、个人歌单、私人审计记录或第三方项目代码/素材。
- Windows 下网易云会话与各渠道可选凭证均使用当前用户的 **DPAPI**（`CryptProtectData`）加密存储于 `%LOCALAPPDATA%\Pruner Studio`，且 `/api/channels` 永不回传原始明文密钥；但同一 Windows 用户上下文仍具备解密权限，请保护好本机账户安全。退出登录或清除渠道凭证会立即移除对应加密数据。
- 严格执行**按渠道凭证隔离与 SSRF 边界**：网易云 Cookie 绝不会发送给任何外部音源或 `amll-ttml-db`；各第三方渠道凭证仅发送至对应受信任 HTTPS 域名或经 `validate_custom_external_api` 校验的用户自建 `api_base`，且跨站重定向一律剥离凭证头；`GDStudio` 始终零凭证；封面代理 `/api/cover` 仅允许 `*.126.net` / `*.music.126.net` HTTPS 地址且不发送 Cookie；AMLL TTML XML 解析禁用 DTD/XXE；`/api/desktop/command` 严格白名单校验动作并要求本机 Origin/Host/Fetch-Metadata 与 JSON Content-Type。
- 不要在 Issue、截图或日志中提交二维码、Cookie、渠道 Token、账号 ID 或个人曲库。更多安全策略见 [SECURITY.md](SECURITY.md)。

## 重要说明

本项目非网易云音乐或任何音乐平台的官方产品，也未获得相关平台授权。它使用公开或社区接口，仅供管理与收听你自己的合法账号与本地内容。使用前请自行阅读并遵守网易云音乐及所配置第三方渠道的现行服务条款；接口变化、账号风控或版权限制都可能导致部分功能失效。

作为同类生态风险的公开信号，常见的第三方接口项目 [Binaryify/NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi) 已于 2024-04-16 归档，并写明因保护版权不再维护。本项目没有复制该项目代码，也不强制依赖它作为额外本机服务。

## License

MIT，见 [LICENSE](LICENSE)。随界面打包的 Marcellus 字体使用 SIL Open Font License，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。各音乐平台名称和商标归其各自权利人所有。
