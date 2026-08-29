# Pruner Studio

Pruner Studio 是一个本机运行的网易云音乐歌单整理工具。扫码登录自己的账号后，它会同步“喜欢的音乐”，让你一边试听、一边快速决定保留或移除；曲库、规则筛选和批量处理都收在次级页面，不打断单曲整理。

## 当前功能

- 扫码登录并记住本机会话；登录后自动同步真实的“喜欢的音乐”。
- 在“整理”页用按钮或键盘快速试听、保留、跳过、取消喜欢或移出当前自建歌单。
- 默认立即前进，但云端写入会等待 3 秒；这段时间按 `Z` 可以撤销。也可以切换为统一确认的批量模式。
- “曲库”提供表格/卡片、搜索、筛选、排序、50 首分页和批量选择；规则只负责预览与暂存，不会绕过确认直接修改云端。
- 支持同步自建歌单，以及 JSON/CSV 离线导入。导入歌曲会保持为本地来源，不能直接触发云端移除。
- 优先播放原曲；原曲不可用时，只接受“标题 + 主艺术家 + 时长”都匹配的替代音源，并明确标记。
- 第一次破坏性操作和每次批量提交前自动创建本地快照，保留最近 10 份；也可手动下载 UTF-8 JSON 备份。
- 只有网易云明确确认成功的歌曲才会从本地移除。网络结果不确定时会把恢复记录写入本机并冻结新的云端写入；即使重启，也只能只读核对或明确忽略，不会盲目重试。

## 使用 Windows 单文件版

从 Releases 下载 `Pruner-Studio.exe`，双击后浏览器会自动打开本机页面（通常是 `http://127.0.0.1:8765/`；若端口已占用会自动顺延）。运行期间请保留启动窗口，关闭它即可退出本机服务。同一数据目录一次只允许一个实例运行，以免会话、曲库或恢复日志互相覆盖。程序只监听本机地址；登录会话、曲库缓存、规则、恢复日志和备份保存在当前 Windows 用户的 `%LOCALAPPDATA%\Pruner Studio`，不会写进程序目录。

首次使用：

1. 双击程序，用网易云音乐手机 App 扫描页面中的二维码。
2. 登录成功后直接进入“整理”；按 `P` 播放，`←/A` 移除，`→/D` 保留，`Z` 撤销尚未提交的移除。
3. 需要搜索、规则筛选或批量处理时，再进入“曲库”或“云端”。

程序直接连接网易云音乐网页接口，不需要另行安装或启动 `NeteaseCloudMusicApi` 等网关服务。

## 从源码运行

需要 Python 3.11+；Node.js 仅用于运行前端测试。

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

打包产物位于 `dist\Pruner-Studio.exe`。

Windows 单文件构建固定使用 64 位 CPython 3.11；`requirements-build.lock` 记录精确 wheel 版本与 SHA-256，`Pruner-Studio.spec` 和统一版本源一并纳入仓库。源码运行仍支持 Python 3.11 及以上版本。

## 隐私与安全边界

- 这是单用户本机工具，不要把端口代理到局域网或公网。
- 仓库不包含任何账号 Cookie、个人歌单、私人审计记录或第三方项目代码/素材。
- 会话以本机用户文件形式保存；任何能读取该 Windows 用户文件的人都可能读取会话。退出登录会删除本工具保存的会话文件。
- 不要在 Issue、截图或日志中提交二维码、Cookie、账号 ID 或个人曲库。

## 重要说明

本项目非网易云音乐官方产品，也未获得网易云音乐授权。它使用未公开、可能随时变化的网页接口，仅供整理你自己的账号内容。使用前请自行阅读并遵守网易云音乐现行服务条款；接口变化、账号风控或版权限制都可能导致部分功能失效。项目不会绕过付费、地域或版权限制。

作为同类生态风险的公开信号，常见的第三方接口项目 [Binaryify/NeteaseCloudMusicApi](https://github.com/Binaryify/NeteaseCloudMusicApi) 已于 2024-04-16 归档，并写明因保护版权不再维护。本项目没有复制该项目代码，也不依赖它作为额外本机服务。

## License

MIT，见 [LICENSE](LICENSE)。随界面打包的 Marcellus 字体使用 SIL Open Font License，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。网易云音乐名称和商标归其权利人所有。
