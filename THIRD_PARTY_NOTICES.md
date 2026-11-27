# Third-party notices

Pruner Studio incorporates or packages the following open-source fonts, libraries, and runtime components. License texts and upstream copyright notices belong to their respective authors.

## Bundled Fonts

### Marcellus
- Location: `web/assets/fonts/Marcellus-Regular.ttf`
- Source: [Google Fonts repository](https://github.com/google/fonts/tree/main/ofl/marcellus)
- Copyright (c) 2012, Brian J. Bonislawsky DBA Astigmatic (AOETI), with Reserved Font Name "Marcellus".
- License: SIL Open Font License, Version 1.1 (`web/assets/fonts/OFL.txt`).

## Python Runtime & Windows Packaging Dependencies

| Component | License | Usage |
| :--- | :--- | :--- |
| **Starlette** (`starlette`) | BSD-3-Clause | Local loopback and LAN ASGI web framework |
| **Uvicorn** (`uvicorn`), **h11**, **anyio**, **click** | BSD-3-Clause / MIT | ASGI HTTP/1.1 & TLS server runtime |
| **HTTPX** (`httpx`), **httpcore**, **certifi**, **idna** | BSD-3-Clause / MPL-2.0 | Isolated HTTP client with DNS pinning and redirect controls |
| **PyCryptodome** (`pycryptodome`) | BSD-2-Clause / Public Domain | AES-GCM WebDAV sync encryption, ECDSA P-256 LAN TLS identity, EAPI/WEAPI signing |
| **Pillow** (`Pillow`) | MIT-CMU | Tray icons, cover/QR image utilities |
| **qrcode** (`qrcode`) | BSD-3-Clause | Login and LAN pairing QR code generation |
| **pywebview** (`pywebview`), **bottle**, **proxy_tools** | BSD-3-Clause / MIT | Native Windows Edge WebView2 desktop shell and floating lyrics window |
| **pystray** (`pystray`) | LGPL-3.0-only | Desktop system tray integration |
| **pythonnet**, **clr_loader**, **cffi**, **pycparser**, **pywin32-ctypes** | MIT / BSD-3-Clause | Windows WinForms / WebView2 .NET bridge |
| **PyInstaller** (`pyinstaller`, `pefile`, `altgraph`) | GPL-2.0-or-later with Bootloader Exception / MIT | Single-file Windows desktop packaging |
| **mutagen** *(optional extra)* | GPL-2.0-or-later | Optional accelerated audio tag extraction when installed by user |
| **keyring** *(optional extra)* | MIT | Optional OS keyring credential storage on macOS/Linux |

## Android Client Dependencies

| Component | License | Usage |
| :--- | :--- | :--- |
| **AndroidX Compose / Material3 / Activity / Core / Lifecycle** | Apache-2.0 | Native Jetpack Compose UI and lifecycle |
| **AndroidX Media3 (ExoPlayer / Session / OkHttp DataSource)** | Apache-2.0 | Background audio playback and media session |
| **OkHttp / OkHttp-SSE** | Apache-2.0 | Pinned-TLS LAN sync HTTP & SSE transport |
| **Coil** (`coil-compose`) | Apache-2.0 | Asynchronous artwork loading and caching |
| **ZXing Core** (`com.google.zxing:core`) | Apache-2.0 | LAN pairing QR scanning/decoding |
| **Kotlinx Coroutines & Serialization** | Apache-2.0 | Concurrency and JSON serialization |
